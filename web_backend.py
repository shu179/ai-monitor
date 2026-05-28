"""Local HTTP backend for the Web UI.

This module exposes the existing Python application's state to a browser-based
UI. It serves both API endpoints and the built frontend assets.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import html
import importlib
import json
import mimetypes
import os
import re
import secrets
import signal
import shutil
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import tempfile
import time
import traceback
from email.utils import parsedate_to_datetime
from collections import Counter
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

try:
    import certifi
except Exception:
    certifi = None  # type: ignore[assignment]

AIHOT_DAILY_PUBLIC_URL = "https://aihot.virxact.com/api/public/daily"
AIHOT_DAILY_FEED_URL = "https://aihot.virxact.com/feed/daily.xml"
AIHOT_DAILY_FEED_CACHE_SECONDS = 30 * 60
AIHOT_DAILY_FEED_MAX_BYTES = 1024 * 1024

from backend_lib.analysis_context import (
    _ASSISTANT_ANALYSIS_ACTION_KEYWORDS,
    _ASSISTANT_ANALYSIS_METRIC_KEYWORDS,
    _ASSISTANT_ANALYSIS_SCOPE_KEYWORDS,
    _ASSISTANT_ANALYSIS_TRIGGER_PHRASES,
    _ASSISTANT_CHAT_MODE_MARKERS,
    _build_monitoring_analysis_payload as _build_monitoring_analysis_payload_impl,
    _build_task_meta_map as _build_task_meta_map_impl,
    _failure_rate,
    _format_assistant_analysis_context,
    _increment_bucket,
    _parse_history_timestamp,
    _rank_failure_buckets,
    _should_attach_assistant_analysis,
)
from backend_lib.article_export import _build_article_export_xlsx
from backend_lib.article_import import (
    _extract_article_import_items,
    _normalize_article_import_date,
    _normalize_article_import_media_type,
    _normalize_import_header,
    _read_article_import_tables,
    _resolve_article_import_platform_label,
    _split_article_import_platform_account,
    _stringify_table_cell,
)
from backend_lib.article_service import (
    ArticleImportBatchStore,
    ArticleService,
    _article_import_batch_to_api,
    _article_import_batches_path,
    _article_import_task_terms,
    _article_published_date,
    _article_to_api,
    _compact_article_import_match_text,
    _dedupe_articles_by_url,
    _load_article_import_batches_file,
    _merge_article_import_matches,
    _normalize_article_import_batch,
    _runtime_article_import_batches,
    _save_article_import_batches_file,
)
from backend_lib.article_reference_service import ArticleReferenceService
from backend_lib.config_provider import RuntimeConfigProvider
from backend_lib.dashboard import (
    _build_dashboard_failed_tasks as _build_dashboard_failed_tasks_impl,
    _build_dashboard_media_stats,
    _build_dashboard_media_stats_monthly,
    _build_dashboard_month_overview,
    _build_dashboard_today_task_summary as _build_dashboard_today_task_summary_impl,
    _build_dashboard_trend,
    _build_media_stats as _build_media_stats_impl,
    _build_task_failure_summary as _build_task_failure_summary_impl,
    _build_task_trend,
    _build_trend_payload,
    _compute_fixed_screenshot_target as _compute_fixed_screenshot_target_impl,
    _collect_today_successful_task_payload as _collect_today_successful_task_payload_impl,
    _collect_failed_modes_today as _collect_failed_modes_today_impl,
    _empty_trend_payload,
    _format_trend_points,
    _range_to_days,
    _summarize_trend_points,
    _task_is_scheduled_for_day as _task_is_scheduled_for_day_impl,
    _weekday_name_from_iso,
)
from backend_lib.home_copy import _build_dashboard_home_copy as _build_dashboard_home_copy_impl
from backend_lib.image_generation_service import (
    ImageGenerationError,
    generate_image_from_config,
    image_generation_config_to_api,
    normalize_image_generation_config,
)
from backend_lib.keyword_import import (
    KEYWORD_IMPORT_EXTENSIONS,
    _decode_plain_text,
    _dedupe_keyword_import_candidates,
    _detect_keyword_import_columns,
    _extract_docx_text_lines,
    _extract_keyword_import_from_tables,
    _extract_keyword_import_from_text_lines,
    _extract_keyword_import_items,
    _extract_legacy_doc_text_lines,
    _match_keyword_import_header,
    _normalize_keyword_import_candidate,
    _split_keyword_import_candidates,
)
from backend_lib.settings_service import SettingsService, _screenshot_theme_to_api
from backend_lib.selector_heal_service import SelectorHealService
from backend_lib.snapshot_fragments import (
    _build_snapshot_assistant,
    _build_snapshot_branding,
    _build_snapshot_dashboard,
    _build_snapshot_monitoring,
    _build_snapshot_platform_items,
    _build_snapshot_profile,
    _build_snapshot_profile_summary,
    _build_snapshot_session,
    _build_snapshot_sidebar,
    _build_snapshot_source_breakdown,
    _build_snapshot_stats,
    _build_snapshot_task_items,
    _collect_snapshot_tag_options,
)
from backend_lib.task_overview_service import TaskOverviewService
from backend_lib.todo_service import TodoService, _todo_to_api
from core import SmartScheduler
from core.app_runtime import BrowserAuthSessionStore, TestRunStateStore
from core.article_history_sqlite_mirror import default_shadow_db_path, rebuild_shadow_store
from core.article_history_sqlite_store import ARTICLE_PAGE_MAX_LIMIT, ArticleHistorySQLiteStore
from core.app_paths import get_app_root, get_data_root, resolve_app_path
from core.account_crawler import (
    crawl_account_articles,
    normalize_account_crawling_settings,
    should_run_scheduled_crawl,
)
from core.article_store import (
    export_article_store_bundle,
    get_article_store_backend_health,
    get_article_source_signature,
    get_articles,
    get_articles_file_path,
    get_excluded_article_urls,
    normalize_article_url,
    refresh_article_matches,
    restore_excluded_article_urls,
    schedule_article_match_refresh,
)
from core.browser_auth import (
    activate_browser_auth_profile,
    build_browser_runtime_diagnostics,
    create_fresh_browser_auth_profile,
    get_browser_auth_snapshot,
    mark_browser_auth_profile_authenticated,
    reset_browser_auth_environment,
)
from core.browser_processes import (
    browser_profile_owner_pids,
    is_browser_profile_in_use,
    pid_is_alive,
    terminate_browser_profile_processes,
    wait_for_pids_exit,
)
from core.browser_platform_factory import (
    BROWSER_PLATFORM_IDS,
    create_browser_platform,
    get_browser_platform_class,
    normalize_browser_platform_name,
)
from core.browser_runtime import resolve_system_browser_executable
from core.batch_test_storage import merge_batch_json, read_batch_json, write_batch_json
from core.cloud_client import CloudClientError, SurfacedCloudClient
from core.cloud_event_types import EVENT_ARTICLE_CHANGED, EVENT_PROFILE_UPDATE, FORCE_SEND_SOURCE
from core.cloud_article_sync import pull_cloud_articles_into_store
from core.cloud_outbox import CloudOutbox
from core.cloud_run_sync import (
    enqueue_profile_update,
    enqueue_task_day_status,
)
from core.cloud_sync_daemon import (
    CloudSyncCommandDaemonProcess,
    DAEMON_SUPPORTED_COMMANDS,
    UnixSocketCloudSyncCommandClient,
    UnixSocketCloudSyncCommandServer,
    build_cloud_sync_socket_path,
)
from core.cloud_sync_runtime import (
    AppCloudRuntimeSupport,
    LocalCloudSyncRuntime,
    create_in_process_cloud_sync_command_client,
    create_local_cloud_sync_runtime,
)
from core.cloud_session_store import (
    CloudSessionChangedError,
    CloudSessionStore,
    cloud_session_identity,
    cloud_session_identity_key,
)
from core.sqlite_retry import call_with_locked_retry
from core.cloud_task_sync import (
    pull_cloud_tasks_into_config,
    refresh_visible_cloud_task_day_statuses_from_history,
)
from core.local_account_space import (
    account_profile_dir_from_session,
    current_account_config_path,
    ensure_current_account_space,
    merge_profile_meta,
)
from core.profile_assets import (
    is_profile_avatar_url,
    profile_avatar_data_url,
    public_profile_avatar_url,
    read_profile_avatar_asset,
    store_profile_avatar,
)
from core.shutdown import install_shutdown_handlers, register_shutdown_callback, run_shutdown_callbacks
from core.context_snapshots import DEFAULT_CONTEXT_SNAPSHOTS_CONFIG, ensure_context_snapshots, get_context_snapshots_config
from core.cycle_state import (
    is_report_success,
    resolve_report_display_message,
    resolve_report_status,
    update_cycle_report_with_forced_success,
)
from core.daily_task_state import (
    assign_task_id,
    build_task_state_extra,
    derive_task_id,
    get_task_day_status,
    get_task_status_label,
    reset_manual_test_session_state,
    write_task_status,
)
from core.logging_utils import redact_secret_text, redact_secrets
from core.diagnostics import get_events
from core.history import (
    configure_structured_history_storage,
    get_task_daily_query_state_bundle,
    get_task_daily_success_bundle,
    get_all_task_names,
    get_brand_trend_series,
    get_records_many,
    get_pending_reviews,
    get_structured_read_health,
    maybe_schedule_structured_history_auto_rebuild,
    get_task_brand_names,
    is_manual_test_failure_record,
    is_success_record,
    reset_structured_read_health,
    save_optimization_period,
)
from core.local_model_manager import get_local_model_manager
from core.notifier import WeComNotifier, send_scheduler_test_message
from core.platform_sessions import (
    PlatformSessionManager,
    build_query_execution_policy,
    build_round_query_plan,
    normalize_query_execution_strategy,
    normalize_session_pool_dispatch,
)
from core.quick_todos import normalize_quick_todos
from core.runtime_state import set_auto_resume_monitoring, should_auto_resume_monitoring
from core.scheduler import describe_task_schedule
from core.scheduler_notifications import SchedulerWebhookReporter
from core.scheduler_state import get_entry as get_scheduler_state_entry
from core.screenshot_tools import get_decoration_theme, get_default_decoration_theme
from core.sync_service import apply_sync_bundle, build_sync_bundle
from core.task_executor import run_task_group
from core.time_utils import local_now, local_today
from core.task_recycle_bin import (
    deleted_task_snapshots,
    mark_task_delete_pending,
    purge_expired_deleted_tasks,
    restore_deleted_task as restore_deleted_task_backup,
    soft_delete_task,
    upsert_deleted_task_backup,
)
from core.task_defaults import compute_recognition_batch_size_from_keywords, get_most_common_task_webhook
from core.update_launcher import build_update_plan_payload, get_runtime_app_dir, launch_updater
from core.update_manager import (
    build_update_status,
    get_app_update_settings,
    normalize_update_channel,
    prepare_update_package,
)
from core.version import APP_NAME, get_http_server_version, get_version_payload
from platforms.api_client import (
    PLATFORM_API_CONFIG,
    get_platform_api_key,
    get_platform_last_error,
    platform_has_configured_access,
    platform_requires_api_key,
    send_platform_chat_messages,
    send_platform_chat_messages_stream,
)

TASKS_FULL_CACHE_TTL_SECONDS = 3.0
TEST_RUN_TERMINAL_TTL_SECONDS = 6 * 60 * 60
SEARCH_FILE_CACHE_TTL_SECONDS = 24 * 60 * 60
BROWSER_AUTH_SESSION_TTL_SECONDS = 6 * 60 * 60
CLOUD_ARTICLE_DEFERRED_REFRESH_RETRY_SECONDS = 2.0
ARTICLE_SQLITE_PAGE_LIMIT_GUARD = ARTICLE_PAGE_MAX_LIMIT

_HISTORY_READ_AUTO_VALUES = {"auto", "sqlite_auto", "sqlite_shadow_auto", "auto_sqlite_shadow"}
_HISTORY_READ_SQLITE_VALUES = {"sqlite_shadow", "sqlite_structured", "structured", "sqlite"}
_HISTORY_READ_DISABLED_VALUES = {"0", "false", "no", "off", "disabled", "json", "file", "files"}
_ARTICLE_READ_AUTO_VALUES = {"", "auto", "sqlite_auto", "sqlite_shadow_auto", "auto_sqlite_shadow"}
_ARTICLE_READ_SQLITE_VALUES = {"sqlite_shadow"}
_ARTICLE_READ_COMPARE_VALUES = {"sqlite_shadow_compare"}
_ARTICLE_READ_DISABLED_VALUES = {"0", "false", "no", "off", "disabled", "json", "file", "files"}
_ARTICLE_MATCH_REFRESH_DEFERRED_REASONS = {"already_running"}


def _article_match_refresh_is_deferred(schedule_result: Any) -> bool:
    if not isinstance(schedule_result, dict):
        return False
    if bool(schedule_result.get("scheduled")):
        return True
    reason = str(schedule_result.get("reason") or "").strip()
    return reason in _ARTICLE_MATCH_REFRESH_DEFERRED_REASONS


# 前端和后端平台 ID 可能不一致，做双向映射
_PLATFORM_ID_ALIASES: dict[str, str] = {
    "local_model": "local_model",
    "local_qwen": "local_model",
    "本地模型": "local_model",
    "本地Qwen": "local_model",
    "本地 Qwen": "local_model",
    "qwen": "tongyi",
    "ernie": "wenxin",
    # 中文平台名 → 英文 ID
    "豆包": "doubao",
    "元宝": "yuanbao",
    "通义千问": "tongyi",
    "文心一言": "wenxin",
    # 大小写变体（前端显示名）
    "deepseek": "deepseek",
    "DeepSeek": "deepseek",
    "kimi": "kimi",
    "Kimi": "kimi",
    "ChatGPT": "chatgpt",
    "chatgpt": "chatgpt",
    "Claude": "claude",
    "Gemini": "gemini",
}
_PLATFORM_ID_REVERSE: dict[str, str] = {
    "local_model": "local_qwen",
    "deepseek": "DeepSeek",
    "kimi": "Kimi",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
}

# 英文 ID → 前端显示名（权威映射）
_PLATFORM_ID_TO_DISPLAY: dict[str, str] = {
    "local_model": "本地模型",
    "doubao": "豆包",
    "deepseek": "DeepSeek",
    "kimi": "Kimi",
    "tongyi": "通义千问",
    "wenxin": "文心一言",
    "yuanbao": "元宝",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
}

_BROWSER_AUTOMATION_RUNTIME_MANAGED_FIELDS: set[str] = {
    "browser_locale",
    "browser_accept_language",
    "browser_timezone_id",
    "browser_user_agent",
    "browser_proxy_server",
    "browser_proxy_username",
    "browser_proxy_password",
    "browser_extra_args",
    "page_stabilize_wait_min_ms",
    "page_stabilize_wait_max_ms",
    "failure_backoff_base_seconds",
    "failure_backoff_max_seconds",
}
_BROWSER_AUTOMATION_BLOCKED_FIELDS: set[str] = {
    "target_url",
    "target_url_aliases",
    "external_chrome_launch_target_url",
    "user_data_dir",
    "context",
    "page",
    "_playwright",
    "_browser_connection",
    "_external_browser_process",
    "_external_browser_port",
}

MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
MAX_SEARCH_UPLOAD_BYTES = 25 * 1024 * 1024
SEARCH_UPLOAD_EXTENSIONS = {".xlsx", ".xlsm"}
SESSION_TOKEN_HEADER = "X-Surfaced-Session-Token"
BATCH_ID_HEX_LENGTH = 32

GET_EXACT_RUNTIME_METHODS = {
    "/api/debug/paths": "get_debug_paths",
    "/api/platforms/keys": "get_platform_keys",
    "/api/browser-auth": "get_browser_auth",
    "/api/tasks/full": "get_tasks_full",
    "/api/tasks/deleted": "get_deleted_tasks",
    "/api/settings": "get_settings",
    "/api/todos": "get_todos",
    "/api/cloud/status": "get_cloud_status",
    "/api/cloud/outbox-diagnostics": "get_cloud_outbox_diagnostics",
    "/api/cloud/sync-health": "get_cloud_sync_health",
    "/api/cloud/sync-health/deep": "get_cloud_sync_health_deep",
    "/api/cloud/state-delta-diagnostics": "get_cloud_state_delta_diagnostics",
    "/api/cloud/admin/tasks": "list_cloud_admin_tasks",
    "/api/cloud/admin/users": "list_cloud_admin_users",
    "/api/cloud/admin/article-classification-jobs": "list_cloud_article_classification_jobs",
    "/api/cloud-sync/status": "get_cloud_sync_status",
    "/api/local-model/status": "get_local_model_status",
    "/api/account-crawling/exclusions": "get_account_crawl_exclusions",
    "/api/recognition/status": "get_recognition_status",
    "/api/assistant/tools": "get_assistant_tools",
    "/api/history-storage/status": "get_history_storage_status",
    "/api/image-generation/config": "get_image_generation_config",
}

GET_EXACT_SESSION_TOKEN_REQUIRED_PATHS = frozenset(GET_EXACT_RUNTIME_METHODS.keys())

_BROWSER_PROFILE_SAFE_ROOTS = (
    "user_data",
    "auth",
    "browser_profiles",
)

POST_JSON_RUNTIME_METHODS = {
    "/api/actions/monitoring": "set_monitoring_enabled",
    "/api/actions/run-selected": "trigger_run_selected",
    "/api/actions/save-task-config": "save_task_config",
    "/api/actions/save-profile": "save_profile",
    "/api/actions/refresh-context-snapshots": "refresh_context_snapshots",
    "/api/actions/test-scheduler-notification-webhook": "test_scheduler_notification_webhook",
    "/api/actions/pick-directory": "pick_directory",
    "/api/actions/check-update": "check_app_update",
    "/api/actions/prepare-update": "prepare_app_update",
    "/api/actions/start-local-update": "start_local_update",
    "/api/local-model/prepare": "prepare_local_model",
    "/api/local-model/test": "test_local_model",
    "/api/platforms/save": "save_platform_config",
    "/api/browser-auth/action": "browser_auth_action",
    "/api/tasks": "create_task",
    "/api/tasks/deleted/restore": "restore_deleted_task",
    "/api/brand-draft/parse": "generate_brand_task_draft",
    "/api/todo-draft/parse": "generate_quick_todos_draft",
    "/api/batch-test/start": "start_batch_test",
    "/api/chat": "chat",
    "/api/search/brand-rank": "run_search_brand_rank",
    "/api/settings": "save_settings",
    "/api/todos": "sync_todos",
    "/api/cloud/login": "login_cloud",
    "/api/cloud/register-admin": "register_cloud_admin",
    "/api/cloud/verify-email": "verify_cloud_email",
    "/api/cloud/resend-email-code": "resend_cloud_email_code",
    "/api/cloud/password-reset/request": "request_cloud_password_reset",
    "/api/cloud/password-reset/confirm": "reset_cloud_password",
    "/api/cloud/logout": "logout_cloud",
    "/api/cloud/flush-outbox": "flush_cloud_outbox",
    "/api/cloud/pull-tasks": "pull_cloud_tasks",
    "/api/cloud/pull-state-delta": "pull_cloud_state_delta",
    "/api/cloud/process-state-delta-inbox": "process_cloud_state_delta_inbox",
    "/api/cloud/admin/create-user": "create_cloud_admin_user",
    "/api/cloud/admin/update-user": "update_cloud_admin_user",
    "/api/cloud/admin/delete-user": "delete_cloud_admin_user",
    "/api/cloud/admin/update-task": "update_cloud_admin_task",
    "/api/cloud/admin/sync-task": "sync_cloud_admin_task",
    "/api/cloud/admin/delete-task": "delete_cloud_admin_task",
    "/api/cloud/admin/restore-task": "restore_cloud_admin_task",
    "/api/cloud/admin/resolve-article-classification-job": "resolve_cloud_article_classification_job",
    "/api/cloud/admin/ignore-article-classification-job": "ignore_cloud_article_classification_job",
    "/api/articles": "import_article",
    "/api/account-crawling/run": "run_account_article_crawl",
    "/api/account-crawling/exclusions/restore": "restore_account_crawl_exclusions",
    "/api/sync/import": "import_sync_bundle",
    "/api/recognition/action": "recognition_action",
    "/api/assistant/action": "assistant_action",
    "/api/history-storage/rebuild-shadow": "rebuild_history_sqlite_shadow",
    "/api/image-generation/generate": "generate_image",
}

PUT_DYNAMIC_RUNTIME_METHODS = (
    ("/api/tasks/", "update_task"),
)

DELETE_DYNAMIC_RUNTIME_METHODS = (
    ("/api/tasks/", "delete_task"),
    ("/api/articles/", "delete_article"),
)

_BROWSER_AUTOMATION_MANAGED_FIELDS = {
    "doubao": {
        *_BROWSER_AUTOMATION_RUNTIME_MANAGED_FIELDS,
        "new_chat_selector",
        "deep_think_menu_trigger_selector",
        "deep_think_think_selector",
        "deep_think_quick_selector",
        # 兼容旧字段，保存时一并清理，避免历史残留继续覆盖。
        "mode_menu_trigger_selector",
        "think_option_selector",
        "quick_option_selector",
    },
    "deepseek": {
        *_BROWSER_AUTOMATION_RUNTIME_MANAGED_FIELDS,
        "new_chat_selector",
        "deep_think_selector",
        "generation_pause_selector",
    },
    "kimi": {
        *_BROWSER_AUTOMATION_RUNTIME_MANAGED_FIELDS,
        "new_chat_selector",
    },
    "yuanbao": {
        *_BROWSER_AUTOMATION_RUNTIME_MANAGED_FIELDS,
        "new_chat_selector",
        "deep_think_selector",
    },
    "tongyi": {
        *_BROWSER_AUTOMATION_RUNTIME_MANAGED_FIELDS,
        "new_chat_selector",
        "deep_think_selector",
    },
    "wenxin": {
        *_BROWSER_AUTOMATION_RUNTIME_MANAGED_FIELDS,
        "new_chat_selector",
        "deep_think_menu_selector",
        "deep_think_enable_item_selector",
    },
}


def _pid_to_display(pid: str) -> str:
    """将平台 ID 转为前端显示名，未知 ID 原样返回。"""
    return _PLATFORM_ID_TO_DISPLAY.get(pid, pid)


def _normalize_platform_id(pid: str) -> str:
    """将前端平台 ID 映射为后端实际 ID。"""
    return _PLATFORM_ID_ALIASES.get(pid, pid)


def _get_platform_config_entry(config: dict[str, Any] | None, pid: str) -> dict[str, Any]:
    platforms_cfg = (config or {}).get("platforms", {}) or {}
    normalized = _normalize_platform_id(str(pid or "").strip())
    entry = platforms_cfg.get(normalized)
    if isinstance(entry, dict):
        return entry
    if normalized == "local_model":
        legacy_entry = platforms_cfg.get("local_qwen")
        if isinstance(legacy_entry, dict):
            return legacy_entry
    return {}


def _merge_browser_automation_config(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """按平台覆盖浏览器自动化配置，并清理前端已托管字段的历史残留值。"""
    merged: dict[str, dict[str, str]] = {}

    def _normalized_platform_patch(raw_platform_cfg: Any) -> dict[str, str]:
        if not isinstance(raw_platform_cfg, dict):
            return {}
        normalized_patch: dict[str, str] = {}
        for raw_key, raw_value in raw_platform_cfg.items():
            key = str(raw_key or "").strip()
            if not key or key in _BROWSER_AUTOMATION_BLOCKED_FIELDS:
                continue
            value = str(raw_value or "").strip()
            if value:
                normalized_patch[key] = value
        return normalized_patch

    for raw_pid, raw_platform_cfg in (existing or {}).items():
        pid = _normalize_platform_id(str(raw_pid or "").strip())
        if not pid:
            continue
        platform_cfg = _normalized_platform_patch(raw_platform_cfg)
        if not platform_cfg:
            continue
        merged[pid] = platform_cfg

    for raw_pid, raw_platform_cfg in (incoming or {}).items():
        pid = _normalize_platform_id(str(raw_pid or "").strip())
        if not pid:
            continue
        managed_keys = _BROWSER_AUTOMATION_MANAGED_FIELDS.get(pid, set())
        preserved = dict(merged.get(pid, {}))
        for key in managed_keys:
            preserved.pop(key, None)
        preserved.update(_normalized_platform_patch(raw_platform_cfg))
        if preserved:
            merged[pid] = preserved
        else:
            merged.pop(pid, None)

    return merged


def _article_read_backend_with_source() -> tuple[str, str, str]:
    raw = os.environ.get("AIBRANDMONITOR_ARTICLE_READ_BACKEND")
    if raw is None:
        return "auto", "default", ""
    raw_text = str(raw or "").strip()
    normalized = raw_text.lower()
    if normalized in _ARTICLE_READ_DISABLED_VALUES:
        return "", "env", raw_text
    if normalized in _ARTICLE_READ_COMPARE_VALUES:
        return "sqlite_shadow_compare", "env", raw_text
    if normalized in _ARTICLE_READ_SQLITE_VALUES:
        return "sqlite_shadow", "env", raw_text
    if normalized in _ARTICLE_READ_AUTO_VALUES:
        return "auto", "env", raw_text
    return "", "env", raw_text


def _read_sqlite_store_meta(db_path: Path, keys: list[str]) -> dict[str, str]:
    if not db_path.exists() or not keys:
        return {}
    normalized_keys = [str(key or "").strip() for key in keys if str(key or "").strip()]
    if not normalized_keys:
        return {}
    placeholders = ", ".join("?" for _ in normalized_keys)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        rows = conn.execute(
            f"SELECT key, value FROM store_meta WHERE key IN ({placeholders})",
            normalized_keys,
        ).fetchall()
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return {str(key or ""): str(value or "") for key, value in rows}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _open_fd_count() -> int | None:
    for path in (Path("/dev/fd"), Path("/proc/self/fd")):
        try:
            return len(list(path.iterdir()))
        except Exception:
            continue
    return None


def _open_sqlite_fd_count(db_path: Path) -> int | None:
    target_paths = {
        str(db_path),
        f"{db_path}-wal",
        f"{db_path}-shm",
    }
    fd_dirs = (Path("/dev/fd"), Path("/proc/self/fd"))
    saw_fd_dir = False
    count = 0
    for fd_dir in fd_dirs:
        try:
            entries = list(fd_dir.iterdir())
        except Exception:
            continue
        saw_fd_dir = True
        for entry in entries:
            try:
                target = os.readlink(entry)
            except Exception:
                continue
            if target in target_paths:
                count += 1
        break
    return count if saw_fd_dir else None


def _sqlite_shadow_db_summary(db_path: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            stat = path.stat()
        except FileNotFoundError:
            files.append({"path": str(path), "exists": False, "bytes": 0})
            continue
        except Exception as exc:
            files.append({
                "path": str(path),
                "exists": False,
                "bytes": 0,
                "error": f"{exc.__class__.__name__}: {exc}",
            })
            continue
        byte_size = int(stat.st_size)
        total_bytes += byte_size
        files.append({
            "path": str(path),
            "exists": True,
            "bytes": byte_size,
            "mtime_ns": int(stat.st_mtime_ns),
        })
    return {
        "path": str(db_path),
        "exists": any(bool(item.get("exists")) for item in files),
        "totalBytes": total_bytes,
        "files": files,
        "openFdCount": _open_sqlite_fd_count(db_path),
    }


def _elapsed_ms_since(started: float) -> float:
    return round((time.perf_counter() - float(started)) * 1000, 3)


def _fd_delta(fd_before: int | None, fd_after: int | None) -> int | None:
    if fd_before is None or fd_after is None:
        return None
    return int(fd_after) - int(fd_before)


def _normalize_cloud_task_id_list(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    task_ids: list[int] = []
    seen: set[int] = set()
    for value in values:
        task_id = _safe_int(value, 0)
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        task_ids.append(task_id)
    return task_ids


def _has_pending_profile_update_for_session(session: dict[str, Any]) -> bool:
    try:
        queue = CloudOutbox().bind_to_session(session)
        return any(
            str(item.get("event_type") or "") == EVENT_PROFILE_UPDATE
            for item in queue.pending(limit=50)
        )
    except Exception:
        return False


def _mask_secret(value: Any, *, visible: int = 4) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("*"):
        return text
    if len(text) <= visible:
        return "*" * len(text)
    return "*" * max(4, len(text) - visible) + text[-visible:]


def _is_masked_secret(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.startswith("*")


def _preserve_masked_secret(incoming: Any, existing: Any) -> str:
    text = str(incoming or "").strip()
    if not text:
        return ""
    if _is_masked_secret(text):
        return str(existing or "").strip()
    return text


def _preserve_nested_api_keys(incoming: Any, existing: Any) -> Any:
    if not isinstance(incoming, dict):
        return incoming
    existing_dict = existing if isinstance(existing, dict) else {}
    result: dict[str, Any] = {}
    for key, value in incoming.items():
        if key == "api_key":
            result[key] = _preserve_masked_secret(value, existing_dict.get(key, ""))
        elif isinstance(value, dict):
            result[key] = _preserve_nested_api_keys(value, existing_dict.get(key, {}))
        else:
            result[key] = value
    return result


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_safe_browser_profile_dir(profile_dir: str | Path) -> Path:
    target = Path(profile_dir).expanduser().resolve()
    safe_roots = tuple(resolve_app_path(root).resolve() for root in _BROWSER_PROFILE_SAFE_ROOTS)
    if not any(_is_relative_to(target, root) for root in safe_roots):
        raise ValueError("浏览器资料夹必须位于应用数据目录内")
    return target


def _validate_browser_target_url(target_url: str) -> str:
    text = str(target_url or "").strip()
    if not text:
        raise ValueError("浏览器启动地址为空")
    if text.startswith("-") or any(char.isspace() or char == "\0" for char in text):
        raise ValueError("浏览器启动地址包含非法字符")
    parsed = urlparse(text)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("浏览器启动地址只允许 https:// URL")
    return text


def _normalize_platform_test_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"idle", "success", "error"} else "idle"


def _sanitize_config_for_api(config: dict[str, Any] | None) -> dict[str, Any]:
    masked = copy.deepcopy(config or {})

    scheduler_cfg = masked.get("scheduler")
    if isinstance(scheduler_cfg, dict):
        scheduler_cfg["notification_webhook_url"] = _mask_secret(
            scheduler_cfg.get("notification_webhook_url", "")
        )

    search_cfg = masked.get("search")
    if isinstance(search_cfg, dict):
        search_cfg["tavily_api_key"] = _mask_secret(search_cfg.get("tavily_api_key", ""))

    app_update_cfg = masked.get("app_update")
    if isinstance(app_update_cfg, dict):
        app_update_cfg["manifest_url"] = str(app_update_cfg.get("manifest_url", "") or "").strip()
        app_update_cfg["download_page_url"] = str(app_update_cfg.get("download_page_url", "") or "").strip()

    tavily_cfg = masked.get("tavily")
    if isinstance(tavily_cfg, dict):
        tavily_cfg["api_key"] = _mask_secret(tavily_cfg.get("api_key", ""))

    cloud_sync_cfg = masked.get("cloud_sync")
    if isinstance(cloud_sync_cfg, dict):
        cloud_sync_cfg["api_token"] = _mask_secret(cloud_sync_cfg.get("api_token", ""))

    platforms_cfg = masked.get("platforms")
    if isinstance(platforms_cfg, dict):
        for platform_cfg in platforms_cfg.values():
            if isinstance(platform_cfg, dict):
                platform_cfg["api_key"] = _mask_secret(platform_cfg.get("api_key", ""))

    tasks = masked.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                task["webhook_url"] = _mask_secret(task.get("webhook_url", ""))

    return masked


def _allowed_cors_origin(handler: BaseHTTPRequestHandler) -> str:
    origin = str(handler.headers.get("Origin", "") or "").strip()
    if not origin:
        return ""

    runtime = getattr(handler, "runtime", None)
    port = getattr(runtime, "port", None)
    allowed = {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }
    if port:
        allowed.add(f"http://127.0.0.1:{port}")
        allowed.add(f"http://localhost:{port}")
    return origin if origin in allowed else ""


def _apply_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    allowed_origin = _allowed_cors_origin(handler)
    if not allowed_origin:
        return
    handler.send_header("Access-Control-Allow-Origin", allowed_origin)
    handler.send_header("Vary", "Origin")


class _RequestRejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _is_allowed_local_host(handler: BaseHTTPRequestHandler) -> bool:
    host_header = str(handler.headers.get("Host", "") or "").strip()
    if not host_header:
        return True
    parsed = urlparse(f"//{host_header}")
    host = str(parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    port = getattr(getattr(handler, "runtime", None), "port", None)
    return not parsed.port or not port or int(parsed.port) == int(port)


def _reject_disallowed_request(handler: BaseHTTPRequestHandler) -> None:
    if not _is_allowed_local_host(handler):
        raise _RequestRejected(HTTPStatus.FORBIDDEN, "forbidden host")
    origin = str(handler.headers.get("Origin", "") or "").strip()
    if origin and not _allowed_cors_origin(handler):
        raise _RequestRejected(HTTPStatus.FORBIDDEN, "forbidden origin")


def _request_session_token(handler: BaseHTTPRequestHandler) -> str:
    header_value = str(handler.headers.get(SESSION_TOKEN_HEADER, "") or "").strip()
    if header_value:
        return header_value
    return str(handler.headers.get("X-CSRF-Token", "") or "").strip()


def _reject_invalid_session_token(handler: BaseHTTPRequestHandler) -> None:
    expected = str(getattr(getattr(handler, "runtime", None), "session_token", "") or "").strip()
    received = _request_session_token(handler)
    if not expected or not received or not secrets.compare_digest(expected, received):
        raise _RequestRejected(HTTPStatus.UNAUTHORIZED, "invalid session token")


def _runtime_timestamp_age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    current = now or local_now()
    try:
        if timestamp.tzinfo is None:
            current = current.replace(tzinfo=None)
        elif current.tzinfo is None:
            current = local_now()
        else:
            timestamp = timestamp.astimezone(current.tzinfo)
        return max(0.0, (current - timestamp).total_seconds())
    except Exception:
        return None


def _local_iso_seconds() -> str:
    return local_now().isoformat(timespec="seconds")


def _normalize_batch_id(batch_id: Any) -> str:
    text = str(batch_id or "").strip()
    if len(text) != BATCH_ID_HEX_LENGTH:
        return ""
    return text if all(char in "0123456789abcdefABCDEF" for char in text) else ""


def _batch_test_file_path(batch_id: Any, suffix: str = "") -> Path | None:
    normalized = _normalize_batch_id(batch_id)
    if not normalized:
        return None
    base_dir = resolve_app_path("user_data/batch_tests").resolve()
    file_path = (base_dir / f"{normalized}{suffix}.json").resolve()
    try:
        file_path.relative_to(base_dir)
    except ValueError:
        return None
    return file_path


def _today_text() -> str:
    return local_today().isoformat()


def _weekday_label(now: datetime | None = None) -> str:
    now = now or local_now()
    return ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]




def _account_context_by_article(config: dict | None) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    settings = normalize_account_crawling_settings(config or {})
    by_id: dict[str, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    for account in settings.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("id", "") or "").strip()
        account_url = str(account.get("url", "") or "").strip()
        if account_id:
            by_id[account_id] = account
        if account_url:
            by_url[account_url] = account
    return by_id, by_url


def _apply_article_account_context(
    article: dict[str, Any],
    account_by_id: dict[str, dict[str, Any]],
    account_by_url: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    enriched = dict(article)
    account_id = str(enriched.get("account_id", "") or "").strip()
    account_url = str(enriched.get("account_url", "") or "").strip()
    account = account_by_id.get(account_id) or account_by_url.get(account_url) or {}
    if not account:
        return enriched

    account_name = str(enriched.get("account_name") or account.get("name") or "").strip()
    platform = str(enriched.get("account_platform") or account.get("platform") or "").strip()
    platform_label = str(enriched.get("account_platform_label") or account.get("platform_label") or "").strip()
    if account_name:
        enriched["account_name"] = account_name
    if platform:
        enriched["account_platform"] = platform
    if platform_label:
        enriched["account_platform_label"] = platform_label

    current_media_name = str(enriched.get("media_name", "") or "").strip()
    if platform_label and (not current_media_name or (account_name and current_media_name == account_name)):
        enriched["media_name"] = platform_label
    current_platform = str(enriched.get("platform", "") or "").strip()
    if platform_label and (not current_platform or (account_name and current_platform == account_name)):
        enriched["platform"] = platform_label
    return enriched


def _apply_articles_account_context(articles: list[dict[str, Any]], config: dict | None) -> list[dict[str, Any]]:
    account_by_id, account_by_url = _account_context_by_article(config)
    if not account_by_id and not account_by_url:
        return articles
    return [
        _apply_article_account_context(article, account_by_id, account_by_url)
        for article in articles
    ]


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _normalize_platform_list(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for platform in _normalize_string_list(values):
        normalized = _normalize_platform_id(platform)
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _normalize_weekdays(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            day = int(value)
        except Exception:
            continue
        if 0 <= day <= 6 and day not in seen:
            result.append(day)
            seen.add(day)
    return sorted(result)


def _normalize_model_options(values: Any) -> list[str]:
    return _normalize_string_list(values)


def _build_available_models(config: dict[str, Any], local_model_status: dict[str, Any] | None = None) -> dict[str, list[str]]:
    available_models: dict[str, list[str]] = {}
    platforms_cfg = config.get("platforms", {}) or {}
    local_cfg = dict(config.get("local_model", {}) or {})
    local_status = local_model_status if isinstance(local_model_status, dict) else {}
    local_runtime_models = _normalize_model_options(local_status.get("models", []))

    for pid, pcfg in PLATFORM_API_CONFIG.items():
        if not platform_has_configured_access(config, pid):
            continue

        user_pcfg = dict(_get_platform_config_entry(config, pid) or {})
        models_list: list[str] = []

        if pid == "local_model":
            candidate_models = [
                str(user_pcfg.get("api_model", "") or "").strip(),
                str(local_cfg.get("default_model", "") or "").strip(),
                *local_runtime_models,
                *_normalize_model_options(user_pcfg.get("model_options", [])),
                str((pcfg or {}).get("default_model", "") or "").strip(),
            ]
        else:
            candidate_models = [
                str(user_pcfg.get("api_model", "") or "").strip(),
                *_normalize_model_options(user_pcfg.get("model_options", [])),
                str((pcfg or {}).get("default_model", "") or "").strip(),
            ]

        for model in candidate_models:
            if model and model not in models_list:
                models_list.append(model)

        if models_list:
            available_models[pid] = models_list

    return available_models


def _normalize_time_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split(":")
    if len(parts) != 2:
        return ""
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        return ""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build_screenshot_theme_patch(payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    return get_default_decoration_theme()


def _schema_type_label(schema: dict[str, Any]) -> str:
    schema_type = str(schema.get("type", "any"))
    if schema_type == "array":
        item_schema = schema.get("items", {}) or {}
        item_type = str(item_schema.get("type", "any"))
        return f"{item_type}[]"
    if "enum" in schema and isinstance(schema["enum"], list):
        enum_values = " | ".join(str(item) for item in schema["enum"])
        return f"{schema_type} ({enum_values})"
    return schema_type


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", []) if isinstance(schema, dict) else [])
    params: dict[str, str] = {}
    for key, value in properties.items():
        label = _schema_type_label(value if isinstance(value, dict) else {})
        if key in required:
            label += " required"
        params[key] = label
    return {
        "name": name,
        "description": description,
        "params": params,
        "schema": schema,
    }


_TASK_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "任务 ID"},
        "task_name": {"type": "string", "description": "任务名称"},
    },
}


_KEYWORD_ITEM_SCHEMA = {
    "type": "object",
    "required": ["keyword"],
    "properties": {
        "keyword": {"type": "string", "description": "关键词文本"},
        "brand": {"type": "string", "description": "关键词品牌，默认沿用任务品牌"},
        "mode": {
            "type": "string",
            "enum": ["browser", "recognition"],
            "description": "关键词运行模式",
        },
        "platforms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "平台 ID 或显示名数组",
        },
        "deep_think_platforms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "开启深度思考的平台数组",
        },
    },
}


ASSISTANT_TOOLS: list[dict[str, Any]] = [
    _tool(
        "set_task_basic_info",
        "修改品牌任务基础信息。优先用于品牌页里的名称、品牌名、Webhook、识别别名。",
        {
            "type": "object",
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "name": {"type": "string", "description": "任务名称"},
                "brand": {"type": "string", "description": "品牌名"},
                "webhook_url": {"type": "string", "description": "企业微信 Webhook"},
                "recognition_brands": {"type": "string", "description": "识别别名，逗号分隔"},
            },
        },
    ),
    _tool(
        "set_task_tags",
        "修改品牌任务的行业标签和地区标签。",
        {
            "type": "object",
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "industry_tags": {"type": "array", "items": {"type": "string"}},
                "region_tags": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    _tool(
        "set_task_schedule",
        "修改品牌任务参与自动运行的星期。weekdays 使用 0-6 表示周一到周日。",
        {
            "type": "object",
            "required": ["weekdays"],
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "weekdays": {"type": "array", "items": {"type": "number"}},
            },
        },
    ),
    _tool(
        "set_task_runtime_options",
        "修改品牌任务开关、检查模式、识别模式、识别批次数和固定截图策略。",
        {
            "type": "object",
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "enabled": {"type": "boolean"},
                "inspect": {"type": "boolean"},
                "recognition_enabled": {"type": "boolean"},
                "recognition_batch_size": {"type": "number"},
                "extract_references_enabled": {"type": "boolean"},
                "fixed_screenshot_enabled": {"type": "boolean"},
                "fixed_screenshot_count": {"type": "number"},
            },
        },
    ),
    _tool(
        "set_task_optimization_period",
        "修改品牌任务优化周期。",
        {
            "type": "object",
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "optimization_start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "optimization_end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
    ),
    _tool(
        "set_task_platforms",
        "重设品牌任务的全局平台，并同步覆盖到所有关键词。",
        {
            "type": "object",
            "required": ["platforms"],
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "platforms": {"type": "array", "items": {"type": "string"}},
                "deep_think_platforms": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    _tool(
        "replace_task_keywords",
        "整组替换品牌任务关键词。仅在用户明确要求整组覆盖时使用；不得用于临时搜索、试搜或查看结果。",
        {
            "type": "object",
            "required": ["keywords"],
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "keywords": {"type": "array", "items": _KEYWORD_ITEM_SCHEMA},
            },
        },
    ),
    _tool(
        "add_task_keyword",
        "为品牌任务新增一个关键词。只有用户明确要求把某个词保存到任务/品牌词库时才能使用；不得用于临时搜索、试搜或查看结果。",
        {
            "type": "object",
            "required": ["keyword"],
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                **_KEYWORD_ITEM_SCHEMA["properties"],
            },
        },
    ),
    _tool(
        "update_task_keyword",
        "修改单个关键词。keyword_index 为 0 基索引。只有用户明确要求修改已存在关键词时才能使用；不得用于临时搜索、试搜或查看结果。",
        {
            "type": "object",
            "required": ["keyword_index"],
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "keyword_index": {"type": "number"},
                **_KEYWORD_ITEM_SCHEMA["properties"],
            },
        },
    ),
    _tool(
        "remove_task_keyword",
        "删除单个关键词。keyword_index 为 0 基索引。只有用户明确要求删除关键词时才能使用。",
        {
            "type": "object",
            "required": ["keyword_index"],
            "properties": {
                **_TASK_REF_SCHEMA["properties"],
                "keyword_index": {"type": "number"},
            },
        },
    ),
    _tool(
        "set_platform_api_key",
        "修改某个平台的 API Key。",
        {
            "type": "object",
            "required": ["platform", "api_key"],
            "properties": {
                "platform": {"type": "string"},
                "api_key": {"type": "string"},
            },
        },
    ),
    _tool(
        "set_platform_primary_model",
        "修改某个平台的主模型名。",
        {
            "type": "object",
            "required": ["platform", "api_model"],
            "properties": {
                "platform": {"type": "string"},
                "api_model": {"type": "string"},
            },
        },
    ),
    _tool(
        "add_platform_model_option",
        "给某个平台追加一个可选模型。",
        {
            "type": "object",
            "required": ["platform", "model"],
            "properties": {
                "platform": {"type": "string"},
                "model": {"type": "string"},
                "make_primary": {"type": "boolean"},
            },
        },
    ),
    _tool(
        "remove_platform_model_option",
        "从某个平台移除一个可选模型。",
        {
            "type": "object",
            "required": ["platform", "model"],
            "properties": {
                "platform": {"type": "string"},
                "model": {"type": "string"},
            },
        },
    ),
    _tool(
        "replace_platform_model_options",
        "整组替换某个平台的模型列表。",
        {
            "type": "object",
            "required": ["platform", "model_options"],
            "properties": {
                "platform": {"type": "string"},
                "model_options": {"type": "array", "items": {"type": "string"}},
                "api_model": {"type": "string"},
            },
        },
    ),
    _tool(
        "test_platform_connection",
        "测试某个平台的 API 连通性。",
        {
            "type": "object",
            "required": ["platform"],
            "properties": {
                "platform": {"type": "string"},
                "api_key": {"type": "string"},
                "model": {"type": "string"},
            },
        },
    ),
    _tool(
        "set_detection_mode",
        "修改全局检测模式。",
        {
            "type": "object",
            "required": ["mode"],
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["browser", "recognition"],
                },
            },
        },
    ),
    _tool(
        "set_scheduler_day_time",
        "修改自动查询时间里的单日开关和时间。",
        {
            "type": "object",
            "required": ["weekday", "enabled"],
            "properties": {
                "weekday": {"type": "number", "description": "0-6 表示周一到周日"},
                "enabled": {"type": "boolean"},
                "time": {"type": "string", "description": "HH:MM"},
            },
        },
    ),
    _tool(
        "set_ai_assistant_model",
        "修改 AI 助手使用的文本大模型。",
        {
            "type": "object",
            "required": ["platform", "model"],
            "properties": {
                "platform": {"type": "string"},
                "model": {"type": "string"},
            },
        },
    ),
    _tool(
        "set_recognition_local_ocr",
        "修改识别模式是否启用本地 OCR。",
        {
            "type": "object",
            "required": ["enabled"],
            "properties": {
                "enabled": {"type": "boolean"},
            },
        },
    ),
    _tool(
        "set_search_provider",
        "修改联网搜索提供方。",
        {
            "type": "object",
            "required": ["provider"],
            "properties": {
                "provider": {"type": "string"},
            },
        },
    ),
    _tool(
        "set_search_tavily_api_key",
        "修改联网搜索的 Tavily API Key。",
        {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string"},
            },
        },
    ),
    _tool(
        "replace_search_model_pool",
        "整组替换搜搜可用的大模型池。",
        {
            "type": "object",
            "required": ["models"],
            "properties": {
                "models": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    _tool(
        "add_search_model",
        "给搜搜大模型池添加一个模型。",
        {
            "type": "object",
            "required": ["model"],
            "properties": {
                "model": {"type": "string"},
            },
        },
    ),
    _tool(
        "remove_search_model",
        "从搜搜大模型池移除一个模型。",
        {
            "type": "object",
            "required": ["model"],
            "properties": {
                "model": {"type": "string"},
            },
        },
    ),
    _tool(
        "set_screenshot_template_basic",
        "修改截图模板基础文案和总开关。",
        {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "footer": {"type": "string"},
            },
        },
    ),
    _tool(
        "set_screenshot_template_toggles",
        "修改截图模板的显示开关。",
        {
            "type": "object",
            "properties": {
                "show_time": {"type": "boolean"},
                "show_footer": {"type": "boolean"},
                "show_highlight": {"type": "boolean"},
            },
        },
    ),
    _tool(
        "set_screenshot_template_colors",
        "修改截图模板颜色。",
        {
            "type": "object",
            "properties": {
                "accent_color": {"type": "string"},
                "background_start": {"type": "string"},
                "background_end": {"type": "string"},
                "header_start": {"type": "string"},
                "header_end": {"type": "string"},
            },
        },
    ),
    _tool(
        "set_screenshot_template_layout",
        "修改截图模板尺寸和圆角。",
        {
            "type": "object",
            "properties": {
                "outer_padding": {"type": "number"},
                "header_height": {"type": "number"},
                "radius": {"type": "number"},
                "image_radius": {"type": "number"},
            },
        },
    ),
    _tool(
        "reset_screenshot_template",
        "把截图模板恢复为默认样式。",
        {
            "type": "object",
            "properties": {},
        },
    ),
    _tool(
        "set_profile_fields",
        "修改系统设置里的账号资料。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "avatar": {"type": "string"},
                "birthday": {"type": "string", "description": "YYYY-MM-DD"},
                "hire_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
    ),
    {
        "name": "run_all_tasks",
        "description": "运行当前所有已启用任务。",
        "params": {},
    },
    {
        "name": "run_selected_tasks",
        "description": "运行指定任务。支持 task_ids 或 task_names。",
        "params": {"task_ids": "string[]", "task_names": "string[]"},
    },
    {
        "name": "create_task",
        "description": "创建新任务。payload 使用 WebUI 任务编辑格式。只有在需要整页创建时才使用。",
        "params": {"payload": "object"},
    },
    {
        "name": "update_task",
        "description": "整页更新已有任务。只有在用户明确要求批量覆盖时才使用，平时优先用更细粒度的 set_task_* 工具。",
        "params": {"task_id": "string", "task_name": "string", "payload": "object"},
    },
    {
        "name": "delete_task",
        "description": "删除任务。支持 task_id 或 task_name。",
        "params": {"task_id": "string", "task_name": "string"},
    },
    {
        "name": "test_run_task",
        "description": "测试运行单个任务。支持 task_id 或 task_name。",
        "params": {"task_id": "string", "task_name": "string"},
    },
    {
        "name": "save_profile",
        "description": "整页更新资料信息。平时优先用 set_profile_fields。",
        "params": {"payload": "object"},
    },
    {
        "name": "save_settings",
        "description": "整页更新系统设置。平时优先用细粒度设置工具，避免参数跑偏。",
        "params": {"payload": "object"},
    },
    {
        "name": "save_platform_config",
        "description": "整页更新平台 API 配置。平时优先用 set_platform_* 工具。",
        "params": {"payload": "object"},
    },
    {
        "name": "recognition_action",
        "description": "控制识别模式。action 可为 start/stop/next/prev/complete/skip。",
        "params": {"action": "string"},
    },
    {
        "name": "sync_todos",
        "description": "同步待办列表，传 todos 数组。",
        "params": {"todos": "object[]"},
    },
    {
        "name": "import_article",
        "description": "通过 URL 导入文章。",
        "params": {"url": "string"},
    },
    {
        "name": "delete_article",
        "description": "删除文章。支持 article_id。",
        "params": {"article_id": "string"},
    },
]


# mode 值到 taskCard key 的映射
_MODE_TO_CARD_KEY = {
    "browser": "capture",
    "recognition": "ocr",
}

_CARD_DEFS = [
    {"key": "capture", "title": "抓取模式", "desc": "快速提取核心数据", "icon": "zap"},
    {"key": "ocr", "title": "识别模式", "desc": "OCR视觉解析", "icon": "eye"},
]


def _build_task_cards(active_mode: str) -> list[dict]:
    """根据全局检测模式决定哪个卡片为活跃。"""
    active_key = _MODE_TO_CARD_KEY.get(active_mode, active_mode)
    return [{**card, "active": card["key"] == active_key} for card in _CARD_DEFS]


# 平台 ID 到中文显示名
_PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "local_model": "本地模型",
    "doubao": "豆包", "deepseek": "DeepSeek", "kimi": "Kimi",
    "tongyi": "通义千问", "wenxin": "文心一言", "yuanbao": "元宝",
    "chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini",
    "ark_deepseek": "Ark DeepSeek", "perplexity": "Perplexity",
}


# 看板检测模式标题 → 内部 mode 值
_MODE_TITLE_TO_KEY: dict[str, str] = {
    "抓取模式": "browser",
    "识别模式": "recognition",
}

_MODE_KEY_TO_TITLE: dict[str, str] = {
    "browser": "抓取模式",
    "recognition": "识别模式",
}


def _collect_failed_modes_today(task: dict) -> tuple[list[str], str, str]:
    return _collect_failed_modes_today_impl(
        task,
        today_text=_today_text(),
        scheduler_state_loader=get_scheduler_state_entry,
        mode_title_by_key=_MODE_KEY_TO_TITLE,
    )


def _task_is_scheduled_for_day(
    task: dict,
    scheduler_config: dict | None,
    target_date: date | None = None,
) -> bool:
    return _task_is_scheduled_for_day_impl(task, scheduler_config, target_date)


def _build_dashboard_today_task_summary(
    tasks: list[dict],
    scheduler_config: dict | None,
    target_date: date | None = None,
) -> dict[str, Any]:
    return _build_dashboard_today_task_summary_impl(tasks, scheduler_config, target_date)


def _build_dashboard_failed_tasks(tasks: list[dict]) -> list[dict]:
    return _build_dashboard_failed_tasks_impl(
        tasks,
        scheduler_state_loader=get_scheduler_state_entry,
        platform_display_name=_pid_to_display,
        mode_title_by_key=_MODE_KEY_TO_TITLE,
        today_text=_today_text(),
    )


def _build_task_failure_summary(task: dict) -> dict:
    return _build_task_failure_summary_impl(
        task,
        scheduler_state_loader=get_scheduler_state_entry,
        platform_display_name=_pid_to_display,
        mode_title_by_key=_MODE_KEY_TO_TITLE,
        today_text=_today_text(),
    )


def _collect_today_successful_task_payload(task: dict) -> dict:
    return _collect_today_successful_task_payload_impl(
        task,
        normalize_string_list=_normalize_string_list,
    )


def _compute_fixed_screenshot_target(task: dict) -> int:
    return _compute_fixed_screenshot_target_impl(task)


def _apply_global_mode(task: dict, global_mode: str) -> dict:
    """将全局检测模式注入任务的每个 keyword，返回新 task dict（不修改原始数据）。"""
    if not global_mode:
        return task
    keywords = task.get("keywords") or []
    new_keywords = [{**kw, "mode": global_mode} for kw in keywords]
    return {**task, "keywords": new_keywords}


def _build_scheduler_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    scheduler_cfg = dict((config or {}).get("scheduler", {}) or {})
    scheduler_cfg["detection_mode"] = str((config or {}).get("detection_mode", "browser") or "browser").strip()
    return scheduler_cfg


def _build_media_stats(platform_counter: Counter) -> list[dict]:
    return _build_media_stats_impl(
        platform_counter,
        platform_display_names=_PLATFORM_DISPLAY_NAMES,
    )


def _build_task_meta_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _build_task_meta_map_impl(
        config,
        normalize_string_list=_normalize_string_list,
    )


def _build_monitoring_analysis_payload(
    config: dict[str, Any],
    articles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _build_monitoring_analysis_payload_impl(
        config,
        articles=articles,
        normalize_string_list=_normalize_string_list,
        platform_display_name=_pid_to_display,
    )


def _build_dashboard_home_copy(
    *,
    config: dict[str, Any],
    mode_key: str,
    monitoring_running: bool,
    recognition_status: dict[str, Any] | None = None,
    enabled_task_count: int = 0,
    total_task_count: int = 0,
    failed_task_count: int = 0,
    today_record_count: int = 0,
    hit_record_count: int = 0,
) -> dict[str, str]:
    return _build_dashboard_home_copy_impl(
        config=config,
        mode_key=mode_key,
        monitoring_running=monitoring_running,
        recognition_status=recognition_status,
        enabled_task_count=enabled_task_count,
        total_task_count=total_task_count,
        failed_task_count=failed_task_count,
        today_record_count=today_record_count,
        hit_record_count=hit_record_count,
        mode_title_by_key=_MODE_KEY_TO_TITLE,
    )


def _normalize_history_read_backend_setting(value: Any, *, default: str = "auto") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _HISTORY_READ_SQLITE_VALUES:
        return "sqlite_shadow"
    if normalized in _HISTORY_READ_AUTO_VALUES or normalized == "":
        return default
    if normalized in _HISTORY_READ_DISABLED_VALUES:
        return ""
    return default


def _session_allows_guarded_history_sqlite(session: dict[str, Any] | None) -> bool:
    if not isinstance(session, dict):
        return True
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    role = str(user.get("role") or "").strip()
    if not role or role == "admin":
        return True
    return not bool(str(session.get("access_token") or "").strip())


def _apply_guarded_history_storage_defaults(
    config: dict[str, Any],
    *,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    storage_cfg = config.get("storage")
    if not isinstance(storage_cfg, dict):
        storage_cfg = {}
    else:
        storage_cfg = dict(storage_cfg)

    if not _session_allows_guarded_history_sqlite(session):
        storage_cfg["history_read_backend"] = ""
        storage_cfg["history_write_backend"] = ""
        storage_cfg["history_shadow_writes_enabled"] = False
    else:
        storage_cfg["history_read_backend"] = _normalize_history_read_backend_setting(
            storage_cfg.get("history_read_backend"),
            default="auto",
        )
        storage_cfg["history_write_backend"] = str(
            storage_cfg.get("history_write_backend") or "auto"
        ).strip().lower()
        storage_cfg["history_shadow_writes_enabled"] = True

    config["storage"] = storage_cfg
    return config


class AppRuntime:
    """Holds live state used by the frontend."""

    def __init__(self) -> None:
        startup_session = CloudSessionStore().load()
        startup_user = startup_session.get("user") if isinstance(startup_session.get("user"), dict) else {}
        ensure_current_account_space(copy_legacy=str(startup_user.get("role") or "").strip() == "admin")
        self.config_path = current_account_config_path()
        self.config_provider = RuntimeConfigProvider(
            self.config_path,
            on_load=self._sync_loaded_config,
        )
        self.frontend_dist = resolve_app_path("web-ui/dist")
        self._lock = threading.RLock()
        self._last_run: dict[str, Any] | None = None
        self._worker: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None
        self.port: int | None = None
        self._recognition_manager: Any | None = None
        self._recognition_test_manager: Any | None = None
        self._recognition_test_runtime_config: dict[str, Any] | None = None
        self._recognition_test_session: dict[str, Any] | None = None
        self._recognition_warmup_thread: threading.Thread | None = None
        self._recognition_warmup_lock = threading.RLock()
        self._test_run_state = TestRunStateStore(
            terminal_ttl_seconds=TEST_RUN_TERMINAL_TTL_SECONDS,
            iso_now_func=_local_iso_seconds,
        )
        self._test_run_lock = self._test_run_state.lock
        self.article_import_batch_store = ArticleImportBatchStore()
        self.todos_service = TodoService(
            config_provider=self.config_provider,
            lock=self._lock,
            normalize_todos=self._normalize_and_store_todos,
        )
        self.article_service = ArticleService(
            config_provider=self.config_provider,
            synced_articles_loader=self._get_synced_articles,
            invalidate_article_cache=self._invalidate_article_cache,
            lock=self._lock,
            import_batch_store=self.article_import_batch_store,
            sqlite_article_page_loader=self._get_sqlite_shadow_article_page,
            sqlite_article_compare_recorder=self._record_sqlite_shadow_article_compare,
        )
        self.article_reference_service = ArticleReferenceService(
            config_provider=self.config_provider,
            synced_articles_loader=self._get_synced_articles,
        )
        self.settings_service = SettingsService(
            context_snapshot_loader=lambda: self._ensure_context_snapshots(refresh_stale=False),
            browser_auth_loader=self.get_browser_auth,
            public_profile_builder=self.get_public_profile,
            cloud_sync_status_getter=lambda: self._cloud_sync_manager.get_status(),
            secret_masker=_mask_secret,
        )
        self.selector_heal_service = SelectorHealService(
            config_loader=self.load_config,
            platform_factory=create_browser_platform,
            selector_config_writer=self._write_selector_heal_config,
            platform_session_closer=lambda platform, reason: self._close_runtime_platform_for_auth(
                platform,
                reason=reason,
            ),
            runtime_safety_checker=self._selector_heal_runtime_safety,
        )
        self._test_failure_notices: dict[str, dict[str, Any]] = {}
        self._context_snapshot_lock = threading.RLock()
        self._article_cache_lock = threading.RLock()
        self._article_sqlite_shadow_lock = threading.RLock()
        self._article_sqlite_shadow_cache_key: tuple[Any, ...] | None = None
        self._article_sqlite_shadow_unready_reason = ""
        self._article_sqlite_shadow_rebuild_lock = threading.RLock()
        self._article_sqlite_shadow_rebuild_state: dict[str, Any] = {}
        self._article_sqlite_shadow_compare_lock = threading.RLock()
        self._article_sqlite_shadow_compare_recent: list[dict[str, Any]] = []
        self._article_sqlite_shadow_health_lock = threading.RLock()
        self._article_sqlite_shadow_health: dict[str, Any] = {}
        self._history_sqlite_shadow_rebuild_lock = threading.RLock()
        self._synced_articles_cache: dict[str, Any] | None = None
        self._pending_delete_processing_lock = threading.RLock()
        self.task_overview_service = TaskOverviewService(
            config_provider=self.config_provider,
            synced_articles_loader=self._get_synced_articles,
            prune_test_failure_notices=self._prune_test_failure_notices,
            test_failure_notice_getter=self._get_test_failure_notice,
            platform_display_name=_pid_to_display,
            secret_masker=_mask_secret,
            normalize_string_list=_normalize_string_list,
            cache_ttl_seconds=TASKS_FULL_CACHE_TTL_SECONDS,
        )
        self._scheduler: SmartScheduler | None = None

        self._account_crawl_thread: threading.Thread | None = None
        self._account_crawl_stop_event = threading.Event()
        self._account_crawl_lock = threading.Lock()
        self._query_session_manager: PlatformSessionManager | None = None
        self._query_session_mode = ""
        self._manual_platform_session_manager: PlatformSessionManager | None = None
        self._manual_platform_lock = threading.RLock()
        self._recognition_browser_lock = threading.RLock()
        self._recognition_browser_started = False
        self._recognition_browser_minimized = False
        self._recognition_browser_debug_port = 0
        self._recognition_browser_profile = resolve_app_path("user_data/recognition_browser_profile")
        self._recognition_browser_tabs: dict[str, str] = {}
        self._recognition_browser_launch_pids: set[int] = set()
        self._browser_auth_state = BrowserAuthSessionStore(
            ttl_seconds=BROWSER_AUTH_SESSION_TTL_SECONDS,
            normalize_platform=normalize_browser_platform_name,
        )
        self._browser_auth_lock = self._browser_auth_state.lock
        self._search_uploads: dict[str, dict[str, Any]] = {}
        self._search_outputs: dict[str, dict[str, Any]] = {}
        self._aihot_daily_feed_lock = threading.RLock()
        self._aihot_daily_feed_cache: dict[str, Any] | None = None
        self._aihot_daily_feed_cached_at = 0.0
        self._aihot_daily_feed_etag = ""
        self._aihot_daily_feed_last_modified = ""
        self._monitoring_status_message = "定时任务已关闭"
        self._exit_callback: Any | None = None
        self._directory_picker_callback: Any | None = None
        self.session_token = secrets.token_urlsafe(32)
        try:
            self._scheduler_reporter = SchedulerWebhookReporter(self.load_config)
        except Exception:
            self._scheduler_reporter = None
        self._cloud_runtime: LocalCloudSyncRuntime = create_local_cloud_sync_runtime(
            config_getter=self.load_config,
            bundle_applier=lambda bundle, mode="merge": self._apply_sync_bundle(bundle, mode=mode),
            config_updater=self._save_runtime_config,
            pull_tasks=lambda force=False: self.pull_cloud_tasks({"force": force}),
            recover_upload_candidates=lambda: self._recover_cloud_run_history_uploads(),
            logger=lambda message: print(redact_secret_text(message)),
        )
        self._cloud_sync_manager = self._cloud_runtime.manager
        self._cloud_platform_auto_sync = self._cloud_runtime.platform_auto_sync
        self._cloud_runtime_support = self._build_cloud_runtime_support()
        self._cloud_platform_auto_sync.set_state_delta_handlers(
            pull_state_delta=self._cloud_runtime_support.pull_cloud_state_delta,
            process_state_delta_inbox=self._cloud_runtime_support.process_cloud_state_delta_inbox,
            retry_object_downloads=self._cloud_runtime_support.retry_object_downloads,
            retry_object_uploads=self._cloud_runtime_support.retry_object_uploads,
        )
        self._cloud_command_transport_lock = threading.RLock()
        self._cloud_command_socket_path: Path | None = None
        self._cloud_command_server: UnixSocketCloudSyncCommandServer | None = None
        self._cloud_command_daemon: CloudSyncCommandDaemonProcess | None = None
        self._cloud_command_client: Any | None = None
        self._cloud_command_transport_mode = "not_started"
        self._cloud_command_transport_error = ""
        self._isolate_ordinary_cloud_account_config(CloudSessionStore().load())

    def _build_cloud_runtime_support(self) -> AppCloudRuntimeSupport:
        return AppCloudRuntimeSupport(
            owner=self,
            session_store_factory=lambda: CloudSessionStore(),
            outbox_factory=lambda: CloudOutbox(),
            thread_factory=lambda *args, **kwargs: threading.Thread(*args, **kwargs),
            sleep_fn=lambda seconds: time.sleep(seconds),
            status_client_factory=lambda base_url: SurfacedCloudClient(base_url, timeout_seconds=3.0),
            request_client_factory=lambda base_url: SurfacedCloudClient(base_url),
            auto_sync_status_getter=lambda: self._cloud_platform_auto_sync.get_status()
            if getattr(self, "_cloud_platform_auto_sync", None)
            else {},
            current_account_config_path_getter=lambda: current_account_config_path(),
            has_pending_profile_update=lambda session: _has_pending_profile_update_for_session(session),
            article_snapshot_startup_delay_seconds=5.0,
            article_deferred_retry_seconds=CLOUD_ARTICLE_DEFERRED_REFRESH_RETRY_SECONDS,
        )

    def _ensure_cloud_runtime_support(self) -> AppCloudRuntimeSupport:
        support = getattr(self, "_cloud_runtime_support", None)
        if isinstance(support, AppCloudRuntimeSupport):
            return support
        support = self._build_cloud_runtime_support()
        self._cloud_runtime_support = support
        platform_auto_sync = getattr(self, "_cloud_platform_auto_sync", None)
        if platform_auto_sync is not None and callable(getattr(platform_auto_sync, "set_state_delta_handlers", None)):
            platform_auto_sync.set_state_delta_handlers(
                pull_state_delta=support.pull_cloud_state_delta,
                process_state_delta_inbox=support.process_cloud_state_delta_inbox,
                retry_object_downloads=support.retry_object_downloads,
                retry_object_uploads=support.retry_object_uploads,
            )
        return support

    def _build_cloud_command_socket_path(self) -> Path:
        scope_hint = f"{Path(self.config_path)}:{os.getpid()}"
        return build_cloud_sync_socket_path(scope_hint)

    def _build_in_process_cloud_command_client(self) -> Any:
        return create_in_process_cloud_sync_command_client(
            self._ensure_cloud_runtime_support().handle_command,
        )

    def _ensure_cloud_command_client(self) -> Any:
        client = getattr(self, "_cloud_command_client", None)
        if client is not None:
            return client
        client = self._build_in_process_cloud_command_client()
        self._cloud_command_client = client
        self._cloud_command_transport_mode = "in_process_direct"
        self._cloud_command_transport_error = ""
        return client

    def _start_cloud_command_transport(self) -> None:
        lock = getattr(self, "_cloud_command_transport_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._cloud_command_transport_lock = lock
        with lock:
            if getattr(self, "_cloud_command_server", None) is not None or getattr(self, "_cloud_command_daemon", None) is not None:
                return
            fallback_client = self._build_in_process_cloud_command_client()
            self._cloud_command_client = fallback_client
            self._cloud_command_socket_path = None
            self._cloud_command_transport_mode = "in_process_direct"
            self._cloud_command_transport_error = ""
            if not hasattr(socket, "AF_UNIX"):
                self._cloud_command_transport_error = "AF_UNIX unavailable"
                return
            socket_path = self._build_cloud_command_socket_path()
            daemon = CloudSyncCommandDaemonProcess(
                socket_path,
                supported_commands=DAEMON_SUPPORTED_COMMANDS,
            )
            try:
                daemon.start()
                client = UnixSocketCloudSyncCommandClient(socket_path)
            except Exception as exc:
                try:
                    daemon.stop()
                except Exception:
                    pass
                self._cloud_command_transport_error = str(exc)
                print(f"[CloudSyncDaemon] Child daemon unavailable, trying in-process socket server: {exc}")
            else:
                self._cloud_command_socket_path = socket_path
                self._cloud_command_daemon = daemon
                self._cloud_command_client = client
                self._cloud_command_transport_mode = "child_daemon"
                self._cloud_command_transport_error = ""
                return
            server = UnixSocketCloudSyncCommandServer(
                socket_path,
                command_handler=self._ensure_cloud_runtime_support().handle_command,
            )
            try:
                server.start()
                client = UnixSocketCloudSyncCommandClient(socket_path)
            except Exception as exc:
                try:
                    server.stop()
                except Exception:
                    pass
                self._cloud_command_transport_error = str(exc)
                print(f"[CloudSyncDaemon] Unix socket transport unavailable, falling back in-process: {exc}")
                return
            self._cloud_command_socket_path = socket_path
            self._cloud_command_server = server
            self._cloud_command_client = client
            self._cloud_command_transport_mode = "in_process_socket"
            self._cloud_command_transport_error = ""

    def _degrade_cloud_command_transport(self, reason: str) -> None:
        """Switch future cloud commands back to the in-process handler."""
        fallback_client = self._build_in_process_cloud_command_client()
        lock = getattr(self, "_cloud_command_transport_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._cloud_command_transport_lock = lock
        reason_text = str(reason or "云同步 daemon 不可用")
        daemon = None
        server = None
        with lock:
            daemon = getattr(self, "_cloud_command_daemon", None)
            server = getattr(self, "_cloud_command_server", None)
            self._cloud_command_daemon = None
            self._cloud_command_server = None
            self._cloud_command_socket_path = None
            self._cloud_command_client = fallback_client
            self._cloud_command_transport_mode = "in_process_direct"
            self._cloud_command_transport_error = reason_text
        if daemon is not None:
            try:
                daemon.stop()
            except Exception:
                pass
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass

    def _stop_cloud_command_transport(self) -> None:
        lock = getattr(self, "_cloud_command_transport_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._cloud_command_transport_lock = lock
        server = None
        daemon = None
        with lock:
            server = getattr(self, "_cloud_command_server", None)
            daemon = getattr(self, "_cloud_command_daemon", None)
            self._cloud_command_server = None
            self._cloud_command_daemon = None
            self._cloud_command_client = None
            self._cloud_command_socket_path = None
            self._cloud_command_transport_mode = "stopped"
        if daemon is not None:
            try:
                daemon.stop()
            except Exception:
                pass
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass

    def _sync_loaded_config(self, config: dict[str, Any]) -> None:
        _apply_guarded_history_storage_defaults(config, session=CloudSessionStore().load())
        get_local_model_manager().sync_config(config)
        configure_structured_history_storage(config)
        storage_cfg = config.get("storage") if isinstance(config.get("storage"), dict) else {}
        if (
            storage_cfg.get("history_read_backend") == "auto"
            or storage_cfg.get("history_write_backend") == "auto"
        ):
            maybe_schedule_structured_history_auto_rebuild("config_load")

    def _activate_current_account_space(self, *, copy_legacy: bool = True) -> None:
        ensure_current_account_space(copy_legacy=copy_legacy)
        next_config_path = current_account_config_path()
        if Path(self.config_path) == next_config_path:
            return
        self.config_path = next_config_path
        self.config_provider.set_path(next_config_path)
        self.article_import_batch_store.reset()
        self._last_run = None
        self._test_run_state.clear()
        self._test_failure_notices.clear()
        self._invalidate_tasks_full_cache()
        self._invalidate_article_cache()
        support = getattr(self, "_cloud_runtime_support", None)
        if isinstance(support, AppCloudRuntimeSupport):
            support.reset_transient_state()

    @staticmethod
    def _cloud_role(session: dict[str, Any] | None) -> str:
        user = session.get("user") if isinstance(session, dict) and isinstance(session.get("user"), dict) else {}
        return str(user.get("role") or "").strip()

    def _login_cloud_account_space(self, *, base_url: str, token_pair: dict[str, Any]) -> dict[str, Any]:
        result = self._cloud_runtime_command(
            "cloud.login_account_space",
            {"base_url": base_url, "token_pair": token_pair},
        )
        session = result.get("session") if isinstance(result, dict) and isinstance(result.get("session"), dict) else {}
        return session

    def _isolate_ordinary_cloud_account_config(self, session: dict[str, Any] | None) -> bool:
        if not isinstance(session, dict):
            return False
        role = self._cloud_role(session)
        if role in {"", "admin"}:
            return False

        profile_dir = account_profile_dir_from_session(session)
        if profile_dir is None:
            return False

        meta_path = profile_dir / "profile_meta.json"
        meta: dict[str, Any] = {}
        try:
            if meta_path.exists():
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta = loaded
        except Exception:
            meta = {}

        config = self.config_provider.load()
        next_config = copy.deepcopy(config or {})
        changed = False
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        desired_profile = {
            "name": str(user.get("username") or "").strip(),
            "role": self._cloud_profile_role_label(role),
            "avatar": "",
            "birthday": str(user.get("birthday") or "").strip(),
            "hire_date": str(user.get("hire_date") or "").strip(),
        }

        profile = next_config.get("profile") if isinstance(next_config.get("profile"), dict) else {}
        if dict(profile or {}) != desired_profile:
            next_config["profile"] = desired_profile
            changed = True

        isolated_at = str(meta.get("ordinary_account_isolated_at") or "").strip()
        needs_one_time_isolation = not isolated_at
        if needs_one_time_isolation:
            tasks = next_config.get("tasks")
            if isinstance(tasks, list):
                filtered_tasks = []
                for task in tasks:
                    if not isinstance(task, dict):
                        continue
                    cloud_task_id = task.get("cloud_task_id") or task.get("cloudTaskId")
                    cloud_task_key = str(task.get("cloud_task_key") or task.get("cloudTaskKey") or "").strip()
                    access_level = str(task.get("cloud_access_level") or "").strip()
                    if not cloud_task_id and not cloud_task_key:
                        continue
                    if access_level == "admin":
                        continue
                    filtered_tasks.append(task)
                if filtered_tasks != tasks:
                    next_config["tasks"] = filtered_tasks
                    changed = True

            for key in ("account_crawling", "scheduler", "search", "cloud_sync", "platforms"):
                if key in next_config:
                    next_config.pop(key, None)
                    changed = True

            meta["ordinary_account_isolated_at"] = local_now().isoformat(timespec="seconds")
            meta["ordinary_account_isolated_role"] = role
            meta["ordinary_account_isolated_user_id"] = user.get("id")
            try:
                merge_profile_meta(profile_dir, meta)
            except Exception:
                pass

        if changed:
            if needs_one_time_isolation:
                self._backup_ordinary_account_config(profile_dir)
            self.config_provider.save(next_config)
        return changed

    @staticmethod
    def _backup_ordinary_account_config(profile_dir: Path) -> None:
        timestamp = local_now().strftime("%Y%m%d-%H%M%S")
        backup_dir = profile_dir / "backups" / f"ordinary-account-isolation-{timestamp}"
        for name in ("config.yaml", "config.local.yaml"):
            source = profile_dir / name
            if not source.exists():
                continue
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, backup_dir / name)
            except Exception:
                pass

    def load_config(self) -> dict:
        self._activate_current_account_space(copy_legacy=False)
        return self.config_provider.load()

    def save_config(self, config: dict[str, Any]) -> Path:
        self._activate_current_account_space(copy_legacy=False)
        return self.config_provider.save(config)

    def _invalidate_tasks_full_cache(self) -> None:
        self.task_overview_service.invalidate_cache()

    def _get_full_task_snapshot(self, task_id: str) -> dict[str, Any]:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return {}
        try:
            payload = self.task_overview_service.get_tasks_full()
            for task in payload.get("tasks", []) or []:
                if isinstance(task, dict) and str(task.get("id") or "").strip() == normalized_task_id:
                    return copy.deepcopy(task)
        except Exception as exc:
            print(f"[WebBackend] 获取任务完整快照失败: task_id={normalized_task_id}, error={exc}")
        return {}

    def _get_cached_task_snapshot(self, task_id: str) -> dict[str, Any]:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return {}
        try:
            getter = getattr(self.task_overview_service, "get_cached_task_snapshot", None)
            if callable(getter):
                snapshot = getter(normalized_task_id)
                if isinstance(snapshot, dict):
                    return snapshot
        except Exception:
            pass
        return {}

    def _get_light_task_snapshot(
        self,
        task_id: str,
        *,
        config: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        base_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return {}

        current_config = config if isinstance(config, dict) else self.load_config()
        if not isinstance(current_config, dict):
            current_config = {}
        target_task = dict(task or {})
        if not target_task:
            for candidate in current_config.get("tasks", []) or []:
                if not isinstance(candidate, dict):
                    continue
                candidate_id = str(candidate.get("task_id") or derive_task_id(candidate)).strip()
                if candidate_id == normalized_task_id:
                    target_task = dict(candidate)
                    break
        if not target_task:
            return copy.deepcopy(base_snapshot) if isinstance(base_snapshot, dict) else {}

        resolved_task_id = str(target_task.get("task_id") or derive_task_id(target_task) or normalized_task_id).strip()
        target_task["task_id"] = resolved_task_id
        task_name = str(target_task.get("name") or resolved_task_id).strip()
        scheduler_config = current_config.get("scheduler", {}) if isinstance(current_config, dict) else {}

        snapshot = (
            copy.deepcopy(base_snapshot)
            if isinstance(base_snapshot, dict)
            else self._get_cached_task_snapshot(resolved_task_id)
        )

        try:
            status = get_task_day_status(target_task)
        except Exception:
            status = {}
        current_status = str(status.get("status") or snapshot.get("status") or "pending").strip() or "pending"
        brand_status = str(status.get("brand_status") or snapshot.get("brand_status") or "pending").strip() or "pending"

        try:
            schedule_text = describe_task_schedule(target_task, scheduler_config)
        except Exception:
            schedule_text = str(snapshot.get("schedule") or "")
        try:
            scheduled_today = _task_is_scheduled_for_day(target_task, scheduler_config, local_today())
        except Exception:
            scheduled_today = bool(snapshot.get("scheduled_today"))
        try:
            failure_summary = _build_task_failure_summary_impl(
                target_task,
                day_status_loader=get_task_day_status,
                platform_display_name=_pid_to_display,
            )
        except Exception:
            failure_summary = {}
        try:
            success_progress = _collect_today_successful_task_payload_impl(
                target_task,
                day_status_loader=get_task_day_status,
                normalize_string_list=_normalize_string_list,
            )
        except Exception:
            success_progress = {}

        completed_keywords_today = [
            str(item).strip()
            for item in (success_progress.get("completedKeywords") or [])
            if str(item).strip()
        ]
        detected_platforms_today = [
            str(item).strip()
            for item in (success_progress.get("detectedPlatforms") or [])
            if str(item).strip()
        ]
        actual_screenshot_count_today = _safe_int(success_progress.get("actualScreenshotCount"), 0)
        fixed_screenshot_target_today = _compute_fixed_screenshot_target_impl(target_task)
        completed_by_quota_today = (
            fixed_screenshot_target_today > 0
            and actual_screenshot_count_today >= fixed_screenshot_target_today
        )

        raw_keywords = [kw for kw in (target_task.get("keywords") or []) if isinstance(kw, dict)]
        raw_platforms: list[Any] = []
        for keyword in raw_keywords:
            raw_platforms.extend(keyword.get("platforms") or [])
        if isinstance(target_task.get("platforms"), list):
            raw_platforms.extend(target_task.get("platforms") or [])
        platforms = sorted({
            _pid_to_display(str(platform or "").strip())
            for platform in raw_platforms
            if str(platform or "").strip()
        })

        keyword_payloads: list[dict[str, Any]] = []
        for keyword in raw_keywords:
            item = copy.deepcopy(keyword)
            item["platforms"] = [
                _pid_to_display(str(platform or "").strip())
                for platform in (keyword.get("platforms") or [])
                if str(platform or "").strip()
            ]
            deep_think = keyword.get("deep_think")
            if isinstance(deep_think, dict):
                item["deep_think"] = {
                    _pid_to_display(str(platform or "").strip()): enabled
                    for platform, enabled in deep_think.items()
                    if str(platform or "").strip()
                }
            elif "deep_think" in item:
                item["deep_think"] = {}
            keyword_payloads.append(item)

        test_failure_notice = None
        try:
            getter = getattr(self, "_get_test_failure_notice", None)
            if callable(getter):
                test_failure_notice = getter(resolved_task_id)
        except Exception:
            test_failure_notice = None
        if not test_failure_notice:
            test_failure_notice = snapshot.get("test_failure_notice")

        total_records = _safe_int(snapshot.get("total_records"), 0)
        success_records = _safe_int(snapshot.get("success_records"), 0)
        success_rate = _safe_float(snapshot.get("success_rate"), 0.0)
        article_count = _safe_int(snapshot.get("article_count"), 0)
        optimization_trend = (
            snapshot.get("optimization_trend")
            if isinstance(snapshot.get("optimization_trend"), list)
            else []
        )

        snapshot.update({
            "id": resolved_task_id,
            "name": task_name,
            "brand": str(target_task.get("brand", "") or "").strip(),
            "enabled": target_task.get("enabled", True),
            "mode": str(current_config.get("detection_mode", "browser") or "browser").strip(),
            "schedule": schedule_text,
            "status": current_status,
            "status_label": get_task_status_label(current_status),
            "brand_status": brand_status,
            "sent_today": bool(status.get("sent_today")),
            "sent_at": str(status.get("sent_at") or ""),
            "scheduled_today": bool(scheduled_today),
            "formal_started": bool(status.get("formal_started")),
            "formal_running": bool(status.get("formal_running")),
            "has_gap": bool(status.get("has_gap")),
            "gap_reasons": list(status.get("gap_reasons") or []),
            "status_source": str(status.get("source") or "").strip(),
            "test_status": str(status.get("test_status") or "").strip(),
            "test_status_source": str(status.get("test_source") or "").strip(),
            "failed_today": bool(failure_summary.get("failedToday", snapshot.get("failed_today", False))),
            "failed_modes_today": list(failure_summary.get("failedModes") or snapshot.get("failed_modes_today") or []),
            "failed_updated_at": str(failure_summary.get("failedUpdatedAt") or snapshot.get("failed_updated_at") or ""),
            "failure_kind_today": str(failure_summary.get("failureKind") or snapshot.get("failure_kind_today") or ""),
            "status_message": str(failure_summary.get("statusMessage") or snapshot.get("status_message") or ""),
            "completed_keywords_today": completed_keywords_today,
            "detected_platforms_today": detected_platforms_today,
            "actual_screenshot_count_today": actual_screenshot_count_today,
            "fixed_screenshot_target_today": fixed_screenshot_target_today,
            "completed_by_quota_today": completed_by_quota_today,
            "test_failure_notice": test_failure_notice,
            "platforms": platforms,
            "keywords": keyword_payloads,
            "webhook_url": _mask_secret(target_task.get("webhook_url", "")),
            "weekdays": target_task.get("weekdays", [0, 1, 2, 3, 4]),
            "industry_tags": target_task.get("industry_tags", []),
            "region_tags": target_task.get("region_tags", []),
            "inspect": bool(target_task.get("inspect", False)),
            "recognition_enabled": bool(target_task.get("recognition_enabled", False)),
            "recognition_brands": target_task.get("recognition_brands", ""),
            "recognition_batch_size": max(1, _safe_int(target_task.get("recognition_batch_size", 3), 3)),
            "extract_references_enabled": bool(target_task.get("extract_references_enabled", False)),
            "fixed_screenshot_enabled": bool(target_task.get("fixed_screenshot_enabled", False)),
            "fixed_screenshot_count": max(
                1,
                _safe_int(
                    target_task.get("fixed_screenshot_count", target_task.get("recognition_batch_size", 3)),
                    1,
                ),
            ),
            "optimization_start_date": target_task.get("optimization_start_date", ""),
            "optimization_end_date": target_task.get("optimization_end_date", ""),
            "created_at": str(target_task.get("created_at") or ""),
            "delete_pending": bool(target_task.get("delete_pending")),
            "delete_pending_at": str(target_task.get("delete_pending_at") or ""),
            "delete_pending_expires_at": str(target_task.get("delete_pending_expires_at") or ""),
            "delete_pending_error": str(target_task.get("delete_pending_error") or ""),
            "cloud_task_id": target_task.get("cloud_task_id"),
            "cloud_task_key": str(target_task.get("cloud_task_key") or ""),
            "cloud_workspace_id": target_task.get("cloud_workspace_id"),
            "cloud_access_level": str(target_task.get("cloud_access_level") or ""),
            "cloud_config_version": target_task.get("cloud_config_version"),
            "cloud_assigned_operator_user_id": target_task.get("cloud_assigned_operator_user_id"),
            "cloud_assigned_operator_username": str(target_task.get("cloud_assigned_operator_username") or ""),
            "cloud_synced_at": str(target_task.get("cloud_synced_at") or ""),
            "total_records": total_records,
            "success_records": success_records,
            "success_rate": success_rate,
            "article_count": article_count,
            "optimization_trend": optimization_trend,
        })
        return snapshot

    def _invalidate_article_cache(self) -> None:
        with self._article_cache_lock:
            self._synced_articles_cache = None
        try:
            self.article_reference_service.invalidate_cache()
        except Exception:
            pass
        self._schedule_cloud_articles_snapshot()

    def _schedule_cloud_articles_snapshot(self) -> None:
        self._cloud_runtime_command("cloud.schedule_article_snapshot")

    def _run_cloud_articles_snapshot_worker(self) -> None:
        self._cloud_runtime_command("cloud.run_article_snapshot_worker")

    def _enqueue_cloud_articles_snapshot(self, config: dict[str, Any] | None = None) -> None:
        self._cloud_runtime_command("cloud.enqueue_article_snapshot", {"config": config})

    def _schedule_cloud_articles_snapshot_retry(self, *, delay_seconds: float | None = None) -> None:
        self._cloud_runtime_command("cloud.schedule_article_snapshot_retry", {"delay_seconds": delay_seconds})

    def _run_cloud_articles_snapshot_retry(self, delay_seconds: float) -> None:
        self._cloud_runtime_command("cloud.run_article_snapshot_retry", {"delay_seconds": delay_seconds})

    def _get_cloud_article_upload_snapshot(
        self,
        config: dict[str, Any] | None = None,
        *,
        session: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        articles, _deferred_refresh = self._get_cloud_article_upload_snapshot_with_refresh_state(
            config,
            session=session,
        )
        return articles

    def _get_cloud_article_upload_snapshot_with_refresh_state(
        self,
        config: dict[str, Any] | None = None,
        *,
        session: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return local article candidates for cloud upload without UI visibility filtering."""
        resolved_config = config or self.config_provider.load()
        session_payload = session if isinstance(session, dict) else CloudSessionStore().load()
        schedule_result = schedule_article_match_refresh(resolved_config, reason="cloud_upload_snapshot")
        deferred_refresh = _article_match_refresh_is_deferred(schedule_result)
        if deferred_refresh:
            articles = _apply_articles_account_context(
                get_articles(),
                resolved_config,
            )
        else:
            articles = _apply_articles_account_context(
                refresh_article_matches(resolved_config),
                resolved_config,
            )
        if self._is_ordinary_cloud_session(session_payload):
            visible_names, visible_cloud_task_names = self._visible_article_task_scope(resolved_config)
            visible_task_name_set = set(visible_names)
            visible_cloud_task_id_set = set(visible_cloud_task_names.keys())
            scoped_articles: list[dict[str, Any]] = []
            for article in articles:
                if not isinstance(article, dict):
                    continue
                item = dict(article)
                item["matched_tasks"] = [
                    task_name
                    for task_name in (
                        str(name or "").strip()
                        for name in (item.get("matched_tasks") or [])
                    )
                    if task_name and task_name in visible_task_name_set
                ]
                raw_reasons = item.get("match_reasons") if isinstance(item.get("match_reasons"), dict) else {}
                item["match_reasons"] = {
                    task_name: raw_reasons.get(task_name, [])
                    for task_name in item["matched_tasks"]
                }
                if isinstance(item.get("cloud_task_ids"), list):
                    item["cloud_task_ids"] = [
                        task_id
                        for task_id in (_safe_int(raw_task_id, 0) for raw_task_id in item.get("cloud_task_ids") or [])
                        if task_id > 0 and task_id in visible_cloud_task_id_set
                    ]
                scoped_articles.append(item)
            return scoped_articles, deferred_refresh
        return articles, deferred_refresh

    @staticmethod
    def _article_cloud_task_map_key(config: dict[str, Any] | None) -> str:
        items: list[dict[str, Any]] = []
        for task in (config or {}).get("tasks") or []:
            if not isinstance(task, dict):
                continue
            items.append({
                "task_id": str(task.get("task_id") or "").strip(),
                "name": str(task.get("name") or "").strip(),
                "brand": str(task.get("brand") or "").strip(),
                "cloud_task_id": _safe_int(task.get("cloud_task_id") or task.get("cloudTaskId"), 0),
            })
        try:
            return json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return ""

    @staticmethod
    def _article_store_version_key() -> tuple[str, int, int]:
        try:
            path = get_articles_file_path()
            try:
                stat = path.stat()
                return (str(path), int(stat.st_mtime_ns), int(stat.st_size))
            except FileNotFoundError:
                return (str(path), 0, 0)
        except Exception:
            return ("", 0, 0)

    @staticmethod
    def _article_match_config_key(config: dict[str, Any] | None) -> str:
        try:
            return json.dumps(
                (config or {}).get("tasks", []) or [],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            return ""

    @staticmethod
    def _article_visibility_session_key(session: dict[str, Any] | None) -> tuple[str, str]:
        return (
            cloud_session_identity_key(session),
            AppRuntime._cloud_role(session),
        )

    @staticmethod
    def _is_ordinary_cloud_session(session: dict[str, Any] | None) -> bool:
        if not isinstance(session, dict):
            return False
        role = AppRuntime._cloud_role(session)
        if not role or role == "admin":
            return False
        return bool(str(session.get("access_token") or "").strip())

    @staticmethod
    def _visible_article_task_scope(config: dict[str, Any] | None) -> tuple[set[str], dict[int, str]]:
        visible_names: set[str] = set()
        visible_cloud_task_names: dict[int, str] = {}
        for task in (config or {}).get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if bool(task.get("delete_pending")):
                continue
            access_level = str(task.get("cloud_access_level") or "").strip().lower()
            if access_level == "revoked":
                continue
            task_name = str(task.get("name") or "").strip()
            brand_name = str(task.get("brand") or "").strip()
            fallback_id = str(task.get("task_id") or derive_task_id(task)).strip()
            display_name = task_name or brand_name or fallback_id
            if not display_name:
                continue
            for candidate in (task_name, brand_name, fallback_id):
                text = str(candidate or "").strip()
                if text:
                    visible_names.add(text)
            cloud_task_id = _safe_int(task.get("cloud_task_id") or task.get("cloudTaskId"), 0)
            if cloud_task_id > 0:
                visible_cloud_task_names[cloud_task_id] = display_name
        return visible_names, visible_cloud_task_names

    def _filter_articles_for_current_cloud_visibility(
        self,
        articles: list[dict[str, Any]],
        config: dict[str, Any] | None,
        *,
        session: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        session_payload = session if isinstance(session, dict) else CloudSessionStore().load()
        if not self._is_ordinary_cloud_session(session_payload):
            return articles

        visible_names, visible_cloud_task_names = self._visible_article_task_scope(config)
        if not visible_names and not visible_cloud_task_names:
            return []

        visible_articles: list[dict[str, Any]] = []
        for article in articles:
            if not isinstance(article, dict):
                continue

            visible_matched: list[str] = []
            cloud_task_ids = [
                _safe_int(item, 0)
                for item in (article.get("cloud_task_ids") or [])
            ]
            for cloud_task_id in cloud_task_ids:
                task_name = visible_cloud_task_names.get(cloud_task_id)
                if task_name and task_name not in visible_matched:
                    visible_matched.append(task_name)

            for raw_name in article.get("matched_tasks") or []:
                task_name = str(raw_name or "").strip()
                if task_name and task_name in visible_names and task_name not in visible_matched:
                    visible_matched.append(task_name)

            if not visible_matched:
                continue

            item = dict(article)
            item["matched_tasks"] = visible_matched
            raw_reasons = item.get("match_reasons") if isinstance(item.get("match_reasons"), dict) else {}
            item["match_reasons"] = {
                task_name: raw_reasons.get(task_name, ["云端可见品牌关联"])
                for task_name in visible_matched
            }
            item["unmatched_reason"] = ""
            visible_articles.append(item)
        return visible_articles

    def _is_browser_auth_session_alive(self, session: dict[str, Any] | None) -> bool:
        if not isinstance(session, dict):
            return False
        if session.get("external_in_use"):
            pid = session.get("pid")
            if isinstance(pid, int) and pid > 0 and self._pid_is_alive(pid):
                return True
            profile_path = str(session.get("profile_path") or "").strip()
            opened_at = str(session.get("opened_at") or "").strip()
            if opened_at and profile_path:
                try:
                    if (
                        (_runtime_timestamp_age_seconds(opened_at) or 0.0) <= 8
                        and self._is_browser_profile_in_use(profile_path)
                    ):
                        return True
                except Exception:
                    pass
            return False
        platform = session.get("platform")
        if platform is None:
            return False
        page = getattr(platform, "page", None)
        if page is None:
            return False
        try:
            return not bool(page.is_closed())
        except Exception as exc:
            print(f"[WebBackend] 检查浏览器登录会话状态失败: {exc}")
            return False

    def _prune_browser_auth_sessions(self) -> None:
        expired_platforms = self._browser_auth_state.expired_platforms()
        for platform_name in expired_platforms:
            self._close_browser_auth_session(
                platform_name,
                reason="登录窗口超过保留时间自动清理",
                graceful_timeout=8.0,
                force=True,
            )

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        return pid_is_alive(pid)

    def _wait_for_profile_pids_exit(self, pids: list[int], timeout_seconds: float) -> bool:
        return wait_for_pids_exit(pids, timeout_seconds)

    def _terminate_browser_profile_processes(
        self,
        profile_path: str,
        *,
        graceful_timeout: float = 8.0,
        force: bool = True,
    ) -> bool:
        return terminate_browser_profile_processes(
            profile_path,
            graceful_timeout=graceful_timeout,
            force=force,
        )

    def _close_browser_auth_session(
        self,
        platform_name: str,
        *,
        reason: str = "",
        graceful_timeout: float = 8.0,
        force: bool = True,
    ) -> None:
        normalized = normalize_browser_platform_name(platform_name)
        if not normalized:
            return
        session = self._browser_auth_state.pop(normalized)
        if not isinstance(session, dict) or not session:
            return
        if session.get("external_in_use"):
            profile_path = str(session.get("profile_path") or "").strip()
            self._terminate_browser_profile_processes(
                profile_path,
                graceful_timeout=graceful_timeout,
                force=force,
            )
            if reason:
                print(f"[WebBackend] 关闭登录窗口: {normalized} ({reason})")
            return
        platform = session.get("platform")
        if platform is None:
            return
        try:
            platform.close()
        except Exception:
            pass
        if reason:
            print(f"[WebBackend] 关闭登录窗口: {normalized} ({reason})")

    def _detach_browser_auth_session(self, platform_name: str, *, reason: str = "") -> None:
        normalized = normalize_browser_platform_name(platform_name)
        if not normalized:
            return
        session = self._browser_auth_state.pop(normalized)
        if not isinstance(session, dict) or not session:
            return
        if reason:
            print(f"[WebBackend] 停止跟踪登录窗口: {normalized} ({reason})")

    def _schedule_browser_auth_session_close(
        self,
        platform_name: str,
        *,
        expected_opened_at: str = "",
        delay_seconds: float = 12.0,
        reason: str = "",
        graceful_timeout: float = 12.0,
        force: bool = False,
    ) -> None:
        normalized = normalize_browser_platform_name(platform_name)
        if not normalized:
            return
        opened_at = str(expected_opened_at or "").strip()

        def _worker() -> None:
            wait_seconds = max(0.0, float(delay_seconds or 0.0))
            if wait_seconds > 0:
                threading.Event().wait(wait_seconds)
            session = self._browser_auth_state.get(normalized)
            current_opened_at = str((session or {}).get("opened_at") or "").strip()
            if not isinstance(session, dict) or not session:
                return
            if opened_at and current_opened_at and current_opened_at != opened_at:
                return
            self._close_browser_auth_session(
                normalized,
                reason=reason or "确认登录后自动关闭",
                graceful_timeout=graceful_timeout,
                force=force,
            )

        thread = threading.Thread(
            target=_worker,
            name=f"browser-auth-close-{normalized}",
            daemon=True,
        )
        thread.start()

    def _schedule_browser_auth_profile_close(
        self,
        profile_path: str,
        *,
        delay_seconds: float = 12.0,
        reason: str = "",
        graceful_timeout: float = 12.0,
        force: bool = True,
    ) -> None:
        target = str(profile_path or "").strip()
        if not target:
            return

        def _worker() -> None:
            wait_seconds = max(0.0, float(delay_seconds or 0.0))
            if wait_seconds > 0:
                threading.Event().wait(wait_seconds)
            self._terminate_browser_profile_processes(
                target,
                graceful_timeout=graceful_timeout,
                force=force,
            )
            if reason:
                print(f"[WebBackend] 关闭登录窗口: {target} ({reason})")

        thread = threading.Thread(
            target=_worker,
            name="browser-auth-close-profile",
            daemon=True,
        )
        thread.start()

    def _close_runtime_platform_for_auth(self, platform_name: str, *, reason: str = "") -> None:
        normalized = normalize_browser_platform_name(platform_name)
        if not normalized:
            return
        if self._query_session_manager is not None:
            try:
                self._query_session_manager.close_session(normalized, reason=reason or "账号环境已切换")
            except Exception:
                pass
        if self._manual_platform_session_manager is not None:
            try:
                self._manual_platform_session_manager.close_session(normalized, reason=reason or "账号环境已切换")
            except Exception:
                pass

    def _get_manual_platform_session_manager(self, config: dict | None = None) -> PlatformSessionManager:
        with self._manual_platform_lock:
            if self._manual_platform_session_manager is not None:
                return self._manual_platform_session_manager
            current = config or self.load_config()
            policy = build_query_execution_policy(current, "browser")
            self._manual_platform_session_manager = PlatformSessionManager(
                "browser",
                policy,
                {platform: 1 for platform in BROWSER_PLATFORM_IDS},
                logger=print,
            )
            return self._manual_platform_session_manager

    def _close_manual_platform_session_manager(self, *, reason: str = "") -> None:
        with self._manual_platform_lock:
            manager = self._manual_platform_session_manager
            self._manual_platform_session_manager = None
        if manager is not None:
            try:
                manager.close_all(reason=reason or "识别模式平台面板已关闭")
            except Exception:
                pass

    @staticmethod
    def _get_current_guide_item_from_manager(manager: Any) -> dict[str, Any] | None:
        getter = getattr(manager, "get_keyword_guide_state", None)
        if not callable(getter):
            return None
        try:
            state = getter() or {}
        except Exception:
            return None
        items = list(state.get("items") or [])
        if not items:
            return None
        try:
            index = int(state.get("index", 0) or 0)
        except Exception:
            index = 0
        index = max(0, min(index, len(items) - 1))
        current = items[index]
        return dict(current) if isinstance(current, dict) else None

    def _open_recognition_platform(self, manager: Any | None, payload: dict[str, Any]) -> dict[str, Any]:
        current_item = self._get_current_guide_item_from_manager(manager) if manager is not None else None
        current_platforms = [
            normalize_browser_platform_name(platform)
            for platform in ((current_item or {}).get("platforms") or [])
            if normalize_browser_platform_name(platform) in BROWSER_PLATFORM_IDS
        ]
        requested = normalize_browser_platform_name(str(payload.get("platform", "") or "").strip())
        platform_name = requested or (current_platforms[0] if current_platforms else "")
        if platform_name not in BROWSER_PLATFORM_IDS:
            return {"ok": False, "message": "请选择要打开的平台" if current_item is None else "当前关键词没有可打开的平台"}

        try:
            self._open_recognition_shared_browser_tab(platform_name)
        except Exception as exc:
            return {"ok": False, "message": f"打开平台失败：{exc}"}

        if manager is not None:
            setter = getattr(manager, "set_active_capture_platform", None)
            if callable(setter):
                try:
                    setter(platform_name)
                except Exception:
                    pass

        keyword = str((current_item or {}).get("keyword") or "").strip()
        return {
            "ok": True,
            "message": f"已打开 {platform_name}" + (f" ({keyword})" if keyword else ""),
            "platform": platform_name,
            "keyword": keyword,
        }

    def _close_recognition_related_browser_profiles(self) -> None:
        self._close_manual_platform_session_manager(reason="切换到识别模式共享浏览器")
        snapshots = get_browser_auth_snapshot(BROWSER_PLATFORM_IDS)
        for platform_name in BROWSER_PLATFORM_IDS:
            self._close_runtime_platform_for_auth(platform_name, reason="切换到识别模式共享浏览器")
            self._close_browser_auth_session(platform_name, reason="切换到识别模式共享浏览器")
            active_profile = (snapshots.get(platform_name) or {}).get("active_profile") or {}
            profile_path = str(active_profile.get("absolute_path") or "").strip()
            if profile_path:
                self._terminate_browser_profile_processes(
                    profile_path,
                    graceful_timeout=5.0,
                    force=True,
                )

    @staticmethod
    def _allocate_local_debug_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            return int(sock.getsockname()[1])

    @staticmethod
    def _same_browser_origin(left_url: str, right_url: str) -> bool:
        try:
            left = urlparse(str(left_url or ""))
            right = urlparse(str(right_url or ""))
        except Exception:
            return False
        if not left.scheme or not right.scheme or not left.netloc or not right.netloc:
            return False
        return left.scheme == right.scheme and left.netloc == right.netloc

    def _recognition_tab_matches_platform_url(self, platform_name: str, tab_url: str, target_url: str) -> bool:
        if self._same_browser_origin(tab_url, target_url):
            return True

        platform_cls = get_browser_platform_class(platform_name)
        if platform_cls is None:
            return False

        candidate_urls = [
            str(target_url or "").strip(),
            str(getattr(platform_cls, "target_url", "") or "").strip(),
            *[
                str(value or "").strip()
                for value in (getattr(platform_cls, "target_url_aliases", None) or [])
            ],
        ]
        normalized_candidates: list[str] = []
        for value in candidate_urls:
            if value and value not in normalized_candidates:
                normalized_candidates.append(value)

        return any(self._same_browser_origin(tab_url, candidate) for candidate in normalized_candidates)

    def _recognition_browser_json(self, path: str, *, timeout: float = 1.0) -> Any | None:
        port = int(self._recognition_browser_debug_port or 0)
        if port <= 0:
            return None
        try:
            request = Request(f"http://127.0.0.1:{port}{path}")
            with urlopen(request, timeout=max(0.2, float(timeout or 1.0))) as response:
                payload = response.read().decode("utf-8", errors="replace")
            return json.loads(payload) if payload else None
        except Exception:
            return None

    def _recognition_browser_is_alive(self) -> bool:
        return isinstance(self._recognition_browser_json("/json/version", timeout=0.5), dict)

    @staticmethod
    def _websocket_frame(opcode: int, payload: bytes = b"") -> bytes:
        mask_key = os.urandom(4)
        first_byte = 0x80 | (int(opcode) & 0x0F)
        length = len(payload)
        header = bytearray([first_byte])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.extend([0x80 | 126])
            header.extend(struct.pack("!H", length))
        else:
            header.extend([0x80 | 127])
            header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        return bytes(header) + mask_key + masked

    @staticmethod
    def _websocket_recv_exact(sock: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = int(size or 0)
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise RuntimeError("WebSocket 连接已关闭")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _websocket_recv_frame(self, sock: socket.socket) -> tuple[int, bool, bytes]:
        header = self._websocket_recv_exact(sock, 2)
        first, second = header[0], header[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._websocket_recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._websocket_recv_exact(sock, 8))[0]
        mask_key = self._websocket_recv_exact(sock, 4) if masked else b""
        payload = self._websocket_recv_exact(sock, int(length)) if length else b""
        if masked and mask_key:
            payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        return opcode, fin, payload

    def _cdp_browser_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 1.5,
    ) -> dict[str, Any] | None:
        version = self._recognition_browser_json("/json/version", timeout=timeout)
        if not isinstance(version, dict):
            return None
        ws_url = str(version.get("webSocketDebuggerUrl") or "").strip()
        if not ws_url:
            return None
        parsed = urlparse(ws_url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return None
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        websocket_guid = "".join(("258EA", "FA5-E", "914-47", "DA-95", "CA-C5", "AB0DC", "85B11"))
        expected_accept = base64.b64encode(
            hashlib.sha1((key + websocket_guid).encode("ascii")).digest()
        ).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")

        try:
            with socket.create_connection((host, port), timeout=max(0.2, float(timeout or 1.5))) as sock:
                sock.settimeout(max(0.2, float(timeout or 1.5)))
                sock.sendall(request)
                response = b""
                while b"\r\n\r\n" not in response and len(response) < 8192:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                header_text = response.decode("iso-8859-1", errors="replace")
                status_line = header_text.split("\r\n", 1)[0]
                if " 101 " not in status_line:
                    return None
                if f"Sec-WebSocket-Accept: {expected_accept}".casefold() not in header_text.casefold():
                    return None

                message = json.dumps(
                    {"id": 1, "method": str(method or "").strip(), "params": params or {}},
                    separators=(",", ":"),
                ).encode("utf-8")
                sock.sendall(self._websocket_frame(0x1, message))

                deadline = time.monotonic() + max(0.2, float(timeout or 1.5))
                fragments: list[bytes] = []
                while time.monotonic() < deadline:
                    opcode, fin, payload = self._websocket_recv_frame(sock)
                    if opcode == 0x8:
                        return None
                    if opcode == 0x9:
                        sock.sendall(self._websocket_frame(0xA, payload))
                        continue
                    if opcode not in {0x0, 0x1}:
                        continue
                    fragments.append(payload)
                    if not fin:
                        continue
                    decoded = b"".join(fragments).decode("utf-8", errors="replace")
                    fragments.clear()
                    result = json.loads(decoded)
                    if isinstance(result, dict) and result.get("id") == 1:
                        return result
        except Exception:
            return None
        return None

    def _recognition_browser_target_id(self) -> str:
        tabs = self._recognition_browser_json("/json/list", timeout=0.7)
        if not isinstance(tabs, list):
            return ""
        cached_tab_ids = {
            str(tab_id or "").strip()
            for tab_id in self._recognition_browser_tabs.values()
            if str(tab_id or "").strip()
        }
        candidates: list[dict[str, Any]] = [tab for tab in tabs if isinstance(tab, dict)]
        for tab in candidates:
            tab_id = str(tab.get("id") or "").strip()
            if tab_id and tab_id in cached_tab_ids:
                return tab_id
        for tab in candidates:
            tab_id = str(tab.get("id") or "").strip()
            tab_type = str(tab.get("type") or "").strip()
            tab_url = str(tab.get("url") or "").strip()
            if tab_id and tab_type == "page" and not tab_url.startswith(("chrome://", "devtools://")):
                return tab_id
        return ""

    def _recognition_browser_window_info(self) -> dict[str, Any] | None:
        target_id = self._recognition_browser_target_id()
        if not target_id:
            return None
        result = self._cdp_browser_command(
            "Browser.getWindowForTarget",
            {"targetId": target_id},
            timeout=1.5,
        )
        payload = result.get("result") if isinstance(result, dict) else None
        if not isinstance(payload, dict):
            return None
        try:
            window_id = int(payload.get("windowId") or 0)
        except Exception:
            window_id = 0
        if window_id <= 0:
            return None
        bounds = payload.get("bounds") if isinstance(payload.get("bounds"), dict) else {}
        return {
            "window_id": window_id,
            "window_state": str((bounds or {}).get("windowState") or "").strip(),
            "target_id": target_id,
        }

    def _recognition_browser_window_state(self) -> str:
        info = self._recognition_browser_window_info()
        if not isinstance(info, dict):
            return ""
        return str(info.get("window_state") or "").strip()

    def _set_recognition_browser_window_state(self, window_state: str) -> bool:
        target_state = str(window_state or "").strip()
        if target_state not in {"normal", "minimized", "maximized", "fullscreen"}:
            return False
        info = self._recognition_browser_window_info()
        if not isinstance(info, dict):
            return False
        window_id = int(info.get("window_id") or 0)
        if window_id <= 0:
            return False
        current_state = str(info.get("window_state") or "").strip()
        if current_state == target_state:
            if target_state == "normal":
                self._activate_existing_browser_window()
            return True
        result = self._cdp_browser_command(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"windowState": target_state}},
            timeout=1.5,
        )
        if not isinstance(result, dict) or result.get("error"):
            return False
        if target_state == "normal":
            self._activate_existing_browser_window()
        return True

    def _wait_recognition_browser_ready(self, *, timeout_seconds: float = 8.0) -> bool:
        deadline = time.monotonic() + max(0.5, float(timeout_seconds or 0.0))
        while time.monotonic() < deadline:
            if self._recognition_browser_is_alive():
                return True
            time.sleep(0.2)
        return False

    def _activate_recognition_tab_by_id(self, tab_id: str) -> bool:
        target_id = str(tab_id or "").strip()
        if not target_id:
            return False
        result = self._recognition_browser_json(f"/json/activate/{target_id}", timeout=0.7)
        if result is None:
            return False
        self._activate_existing_browser_window()
        return True

    def _activate_existing_recognition_tab(self, platform_name: str, target_url: str) -> bool:
        normalized = normalize_browser_platform_name(platform_name)
        cached_tab_id = str(self._recognition_browser_tabs.get(normalized) or "").strip()
        if cached_tab_id and self._activate_recognition_tab_by_id(cached_tab_id):
            return True
        tabs = self._recognition_browser_json("/json/list")
        if not isinstance(tabs, list):
            return False
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            tab_id = str(tab.get("id") or "").strip()
            tab_url = str(tab.get("url") or "").strip()
            if not tab_id or not self._recognition_tab_matches_platform_url(normalized, tab_url, target_url):
                continue
            self._recognition_browser_tabs[normalized] = tab_id
            self._activate_recognition_tab_by_id(tab_id)
            return True
        return False

    def _open_new_recognition_tab(
        self,
        browser_executable: str,
        profile_dir: Path,
        target_url: str,
        *,
        platform_name: str,
        first_open: bool,
    ) -> None:
        profile_dir = _resolve_safe_browser_profile_dir(profile_dir)
        target_url = _validate_browser_target_url(target_url)
        if not first_open and self._recognition_browser_is_alive():
            tab = self._recognition_browser_json(f"/json/new?{quote(target_url, safe='')}", timeout=1.5)
            if isinstance(tab, dict) and str(tab.get("id") or "").strip():
                tab_id = str(tab.get("id") or "").strip()
                self._recognition_browser_tabs[normalize_browser_platform_name(platform_name)] = tab_id
                self._activate_recognition_tab_by_id(tab_id)
                return

        launch_args = [
            browser_executable,
            f"--user-data-dir={profile_dir}",
            f"--remote-debugging-port={self._recognition_browser_debug_port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window" if first_open else "--new-tab",
            target_url,
        ]
        process = subprocess.Popen(
            launch_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if int(process.pid or 0) > 0:
            self._recognition_browser_launch_pids.add(int(process.pid))

    def _open_recognition_shared_browser_tab(self, platform_name: str) -> None:
        normalized = normalize_browser_platform_name(platform_name)
        target_url = self._resolve_browser_auth_login_url(normalized)
        if not target_url:
            raise RuntimeError(f"未找到平台地址：{platform_name}")
        target_url = _validate_browser_target_url(target_url)
        browser_executable = resolve_system_browser_executable()
        if not browser_executable:
            raise RuntimeError(
                "未找到系统正式版 Chrome。请安装 Google Chrome，或设置环境变量 "
                "CHROME_EXECUTABLE_PATH 指向正式版 Chrome 可执行文件。"
            )

        with self._recognition_browser_lock:
            profile_dir = Path(self._recognition_browser_profile)
            profile_dir.mkdir(parents=True, exist_ok=True)
            first_open = (not self._recognition_browser_started) or (not self._recognition_browser_is_alive())
            if first_open:
                self._close_recognition_related_browser_profiles()
                self._terminate_browser_profile_processes(
                    str(profile_dir),
                    graceful_timeout=5.0,
                    force=True,
                )
                self._recognition_browser_debug_port = self._allocate_local_debug_port()
                self._recognition_browser_tabs.clear()

            if not first_open and self._activate_existing_recognition_tab(normalized, target_url):
                return

            self._open_new_recognition_tab(
                browser_executable,
                profile_dir,
                target_url,
                platform_name=normalized,
                first_open=first_open,
            )
            self._recognition_browser_started = True
            if first_open:
                self._wait_recognition_browser_ready(timeout_seconds=8.0)
                self._activate_existing_recognition_tab(normalized, target_url)
            self._recognition_browser_minimized = False
        self._activate_existing_browser_window()

    def _terminate_recognition_browser_launch_pids(self) -> bool:
        pids = [pid for pid in self._recognition_browser_launch_pids if int(pid or 0) > 0]
        if not pids:
            return False
        terminated = False
        for pid in pids:
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=4,
                        check=False,
                    )
                else:
                    os.killpg(pid, signal.SIGTERM)
                terminated = True
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                    terminated = True
                except Exception:
                    pass
        wait_for_pids_exit(pids, 2.0)
        for pid in pids:
            if not pid_is_alive(pid):
                self._recognition_browser_launch_pids.discard(pid)
        return terminated

    def _close_recognition_shared_browser(self) -> None:
        with self._recognition_browser_lock:
            profile_dir = Path(self._recognition_browser_profile)
            closed_by_profile = self._terminate_browser_profile_processes(
                str(profile_dir),
                graceful_timeout=3.0,
                force=True,
            )
            closed_by_pid = self._terminate_recognition_browser_launch_pids()
            if not closed_by_profile and not closed_by_pid:
                print(f"[WebBackend] 未找到识别模式共享浏览器进程: {profile_dir}")
            self._recognition_browser_started = False
            self._recognition_browser_minimized = False
            self._recognition_browser_debug_port = 0
            self._recognition_browser_tabs.clear()
            self._recognition_browser_launch_pids.clear()

    def _trigger_recognition_screenshot(self) -> dict[str, Any]:
        config = self.load_config()
        recognition_cfg = (config.get("recognition", {}) or {})
        if bool(recognition_cfg.get("dom_render_mode", False)):
            return {"ok": False, "message": "当前为 DOM 文本识别模式，请直接复制文本内容"}
        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["screencapture", "-i", "-c"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            elif sys.platform == "win32":
                subprocess.Popen(
                    ["explorer.exe", "ms-screenclip:"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                return {"ok": False, "message": "当前仅 macOS / Windows 桌面版支持直接调用系统截图"}
        except Exception as exc:
            return {"ok": False, "message": f"系统截图启动失败：{exc}"}
        return {"ok": True, "message": "系统截图已启动"}

    def _is_browser_profile_in_use(self, profile_path: str) -> bool:
        return is_browser_profile_in_use(profile_path)

    def _browser_profile_owner_pids(self, profile_path: str) -> list[int]:
        return browser_profile_owner_pids(profile_path)

    @staticmethod
    def _path_mtime_iso(path: Path | None) -> str:
        if path is None or not path.exists():
            return ""
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        except Exception:
            return ""

    @staticmethod
    def _resolve_browser_auth_artifact_path(profile_path: str, candidates: list[str]) -> Path | None:
        target = str(profile_path or "").strip()
        if not target:
            return None
        base = Path(target)
        for relative in candidates:
            candidate = base / relative
            if candidate.exists():
                return candidate
        if not candidates:
            return None
        return base / candidates[0]

    def _build_browser_auth_debug_snapshot(
        self,
        snapshot: dict[str, Any] | None,
        session: dict[str, Any] | None,
        *,
        tracked_open: bool,
        profile_busy: bool,
    ) -> dict[str, Any]:
        payload = snapshot if isinstance(snapshot, dict) else {}
        active_profile = payload.get("active_profile") or {}
        profile_path = self._browser_auth_profile_path(payload)
        raw_profile_in_use = self._is_browser_profile_in_use(profile_path)
        owner_pids = self._browser_profile_owner_pids(profile_path) if profile_path else []
        cookies_path = self._resolve_browser_auth_artifact_path(
            profile_path,
            ["Default/Cookies", "Default/Network/Cookies", "Cookies"],
        )
        login_data_path = self._resolve_browser_auth_artifact_path(
            profile_path,
            ["Default/Login Data", "Login Data"],
        )
        auth_state = str(active_profile.get("auth_state") or "").strip()
        authenticated = bool(active_profile.get("authenticated")) or auth_state == "authenticated"

        if authenticated:
            if tracked_open:
                diagnosis = "已确认登录成功，程序仍在跟踪这个登录窗口。"
            elif profile_busy:
                diagnosis = "已确认登录成功，但该资料夹仍被浏览器或后台进程占用；这不代表前台一定还有登录窗口。"
            else:
                diagnosis = "已确认登录成功，程序后续会直接复用这套浏览器资料夹。"
        elif auth_state == "needs_confirmation":
            if tracked_open:
                diagnosis = "登录页仍在程序追踪中，完成后点“已完成登录”即可保存这套账号环境。"
            elif profile_busy:
                diagnosis = "该资料夹正被浏览器或后台进程占用；如果前台没看到窗口，可点“关闭窗口”清理残留后再重新打开登录页。"
            elif cookies_path and cookies_path.exists():
                diagnosis = "检测到该资料夹已有 Cookies，但还没有在程序里点“确认已登录”。"
            else:
                diagnosis = "检测到该资料夹已有浏览器数据，但还没有形成明确的登录确认状态。"
        elif tracked_open:
            diagnosis = "程序当前仍在跟踪这个登录窗口。"
        elif profile_busy:
            diagnosis = "该资料夹当前被浏览器或后台进程占用；这不等于前台一定还有登录窗口。若没看到窗口，可点“关闭窗口”清理。"
        elif cookies_path and cookies_path.exists():
            diagnosis = "检测到 Cookies 文件，但程序当前仍判定未登录，可能是会话无效、站点要求二次验证，或登录尚未在程序内确认。"
        else:
            diagnosis = "还没有检测到该平台的登录 Cookies，当前更像是尚未在程序资料夹里真正完成登录。"

        return {
            "profile_path": profile_path,
            "tracked_open": bool(tracked_open),
            "external_open": bool(tracked_open and (session or {}).get("external_in_use")),
            "raw_profile_in_use": bool(raw_profile_in_use),
            "owner_pids": owner_pids,
            "cookies_path": str(cookies_path or ""),
            "cookies_exists": bool(cookies_path and cookies_path.exists()),
            "cookies_updated_at": self._path_mtime_iso(cookies_path),
            "login_data_path": str(login_data_path or ""),
            "login_data_exists": bool(login_data_path and login_data_path.exists()),
            "login_data_updated_at": self._path_mtime_iso(login_data_path),
            "diagnosis": diagnosis,
            "auth_state": auth_state,
            "authenticated": authenticated,
            "session_external_in_use": bool((session or {}).get("external_in_use")),
        }

    def _recognition_shared_browser_pids(self) -> list[int]:
        candidates: list[int] = []
        candidates.extend(int(pid) for pid in self._recognition_browser_launch_pids if int(pid or 0) > 0)
        candidates.extend(int(pid) for pid in browser_profile_owner_pids(self._recognition_browser_profile) if int(pid or 0) > 0)
        pids: list[int] = []
        seen: set[int] = set()
        for pid in candidates:
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            pids.append(pid)
        return pids

    def _activate_existing_browser_window(self) -> bool:
        if sys.platform == "win32":
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", "chrome"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
                return True
            except Exception:
                return False
        if sys.platform != "darwin":
            return False
        scripts = (
            'tell application "Google Chrome" to activate',
        )
        activated = False
        for script in scripts:
            try:
                completed = subprocess.run(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
            except Exception:
                continue
            activated = activated or completed.returncode == 0
        return activated

    def _minimize_recognition_shared_browser(self) -> bool:
        if self._set_recognition_browser_window_state("minimized"):
            return True

        pids = self._recognition_shared_browser_pids()
        if not pids:
            return False

        if sys.platform == "win32":
            pid_list = ",".join(str(pid) for pid in pids)
            script = rf"""
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Win32WindowTools {{
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  public static int MinimizeForPids(string csv) {{
    var targets = new HashSet<uint>();
    foreach (var part in csv.Split(',')) {{ if (uint.TryParse(part, out var pid)) targets.Add(pid); }}
    int count = 0;
    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {{
      uint pid; GetWindowThreadProcessId(hWnd, out pid);
      if (targets.Contains(pid) && IsWindowVisible(hWnd)) {{ if (ShowWindowAsync(hWnd, 6)) count++; }}
      return true;
    }}, IntPtr.Zero);
    return count;
  }}
}}
"@
$count = [Win32WindowTools]::MinimizeForPids('{pid_list}')
if ($count -le 0) {{
  '{pid_list}'.Split(',') | ForEach-Object {{
    $pidValue = [int]$_
    Get-Process -Id $pidValue -ErrorAction SilentlyContinue | ForEach-Object {{
      if ($_.MainWindowHandle -ne 0) {{ [Win32WindowTools]::ShowWindowAsync($_.MainWindowHandle, 6) | Out-Null; $script:count++ }}
    }}
  }}
}}
if ($count -gt 0) {{ exit 0 }} else {{ exit 2 }}
"""
            try:
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                return completed.returncode == 0
            except Exception:
                return False

        if sys.platform == "darwin":
            pid_list = ", ".join(str(pid) for pid in pids)
            script = f"""
set targetPids to {{{pid_list}}}
set changedCount to 0
tell application "System Events"
  repeat with targetPid in targetPids
    try
      set targetProcess to first process whose unix id is (targetPid as integer)
      repeat with targetWindow in windows of targetProcess
        try
          set value of attribute "AXMinimized" of targetWindow to true
          set changedCount to changedCount + 1
        end try
      end repeat
      try
        set visible of targetProcess to false
        set changedCount to changedCount + 1
      end try
    end try
  end repeat
end tell
return changedCount
"""
            try:
                completed = subprocess.run(
                    ["osascript", "-e", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=3,
                    check=False,
                )
                return completed.returncode == 0 and int(str(completed.stdout or "0").strip() or "0") > 0
            except Exception:
                return False
        return False

    def _restore_recognition_shared_browser(self) -> bool:
        window_state = self._recognition_browser_window_state()
        if window_state and window_state != "minimized":
            self._recognition_browser_minimized = False
            return self._activate_existing_browser_window() or True
        if self._set_recognition_browser_window_state("normal"):
            return True

        pids = self._recognition_shared_browser_pids()
        if not pids:
            return False

        if sys.platform == "win32":
            pid_list = ",".join(str(pid) for pid in pids)
            script = rf"""
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Win32WindowTools {{
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  public static int RestoreForPids(string csv) {{
    var targets = new HashSet<uint>();
    foreach (var part in csv.Split(',')) {{ if (uint.TryParse(part, out var pid)) targets.Add(pid); }}
    int count = 0;
    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {{
      uint pid; GetWindowThreadProcessId(hWnd, out pid);
      if (targets.Contains(pid) && IsWindowVisible(hWnd)) {{
        if (ShowWindowAsync(hWnd, 9)) count++;
        SetForegroundWindow(hWnd);
      }}
      return true;
    }}, IntPtr.Zero);
    return count;
  }}
}}
"@
$count = [Win32WindowTools]::RestoreForPids('{pid_list}')
if ($count -le 0) {{
  '{pid_list}'.Split(',') | ForEach-Object {{
    $pidValue = [int]$_
    Get-Process -Id $pidValue -ErrorAction SilentlyContinue | ForEach-Object {{
      if ($_.MainWindowHandle -ne 0) {{
        [Win32WindowTools]::ShowWindowAsync($_.MainWindowHandle, 9) | Out-Null
        [Win32WindowTools]::SetForegroundWindow($_.MainWindowHandle) | Out-Null
        $script:count++
      }}
    }}
  }}
}}
if ($count -gt 0) {{ exit 0 }} else {{ exit 2 }}
"""
            try:
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                return completed.returncode == 0
            except Exception:
                return self._activate_existing_browser_window()

        if sys.platform == "darwin":
            pid_list = ", ".join(str(pid) for pid in pids)
            script = f"""
set targetPids to {{{pid_list}}}
set changedCount to 0
tell application "System Events"
  repeat with targetPid in targetPids
    try
      set targetProcess to first process whose unix id is (targetPid as integer)
      set visible of targetProcess to true
      set frontmost of targetProcess to true
      repeat with targetWindow in windows of targetProcess
        try
          set value of attribute "AXMinimized" of targetWindow to false
          perform action "AXRaise" of targetWindow
          set changedCount to changedCount + 1
        end try
      end repeat
    end try
  end repeat
end tell
return changedCount
"""
            try:
                completed = subprocess.run(
                    ["osascript", "-e", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=3,
                    check=False,
                )
                return completed.returncode == 0 and int(str(completed.stdout or "0").strip() or "0") > 0
            except Exception:
                return False
        return self._activate_existing_browser_window()

    def _toggle_recognition_shared_browser(self) -> tuple[str, bool]:
        with self._recognition_browser_lock:
            window_state = self._recognition_browser_window_state()
            should_restore = window_state == "minimized" or (not window_state and self._recognition_browser_minimized)
            if should_restore:
                restored = self._restore_recognition_shared_browser()
                if restored:
                    self._recognition_browser_minimized = False
                return "restore", restored

            minimized = self._minimize_recognition_shared_browser()
            if minimized:
                self._recognition_browser_minimized = True
            return "minimize", minimized

    def _browser_auth_profile_path(self, snapshot: dict[str, Any] | None) -> str:
        payload = snapshot if isinstance(snapshot, dict) else {}
        active_profile = payload.get("active_profile") or {}
        return str(active_profile.get("absolute_path") or "").strip()

    @staticmethod
    def _resolve_browser_auth_login_url(platform_name: str) -> str:
        platform_class = get_browser_platform_class(platform_name)
        if platform_class is None:
            return ""
        return str(getattr(platform_class, "target_url", "") or "").strip()

    def _launch_external_browser_auth_window(self, platform_name: str, profile_path: str) -> int | None:
        normalized = normalize_browser_platform_name(platform_name)
        browser_executable = resolve_system_browser_executable()
        if not browser_executable:
            raise RuntimeError(
                "未找到系统正式版 Chrome。请安装 Google Chrome，或设置环境变量 "
                "CHROME_EXECUTABLE_PATH 指向正式版 Chrome 可执行文件。"
            )
        target_url = self._resolve_browser_auth_login_url(normalized)
        if not target_url:
            raise RuntimeError(f"未找到平台登录地址：{platform_name}")
        target_url = _validate_browser_target_url(target_url)
        profile_path_text = str(profile_path or "").strip()
        if not profile_path_text:
            raise RuntimeError("浏览器账号环境目录无效")
        profile_dir = _resolve_safe_browser_profile_dir(profile_path_text)
        profile_dir.mkdir(parents=True, exist_ok=True)
        launch_args = [
            browser_executable,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            target_url,
        ]
        process = subprocess.Popen(
            launch_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return process.pid

    def _mark_external_browser_auth_session(
        self,
        platform_name: str,
        *,
        profile_id: str = "",
        profile_path: str = "",
        pid: int | None = None,
    ) -> None:
        normalized = normalize_browser_platform_name(platform_name)
        if not normalized:
            return
        self._browser_auth_state.set(normalized, {
            "platform": None,
            "opened_at": local_now().isoformat(timespec="seconds"),
            "profile_id": str(profile_id or "").strip(),
            "profile_path": str(profile_path or "").strip(),
            "pid": int(pid) if isinstance(pid, int) and pid > 0 else None,
            "external_in_use": True,
        })

    @staticmethod
    def _looks_like_existing_browser_session_error(exc: Exception) -> bool:
        text = str(exc or "")
        return "正在现有的浏览器会话中打开" in text or "Opening in existing browser session" in text

    def get_browser_auth(self) -> dict[str, Any]:
        self._prune_browser_auth_sessions()
        snapshots = get_browser_auth_snapshot(BROWSER_PLATFORM_IDS)
        stale_sessions = self._browser_auth_state.pop_where(
            lambda _platform_name, session: not self._is_browser_auth_session_alive(session)
        )
        for _platform_name, session in stale_sessions:
            try:
                if isinstance(session, dict) and session.get("platform") is not None:
                    session["platform"].close()
            except Exception:
                pass
        for platform_name, snapshot in snapshots.items():
            session = self._browser_auth_state.get(platform_name)
            tracked_open = self._is_browser_auth_session_alive(session)
            profile_busy = self._is_browser_profile_in_use(self._browser_auth_profile_path(snapshot))
            snapshot["profile_busy"] = bool(profile_busy)
            snapshot["debug"] = self._build_browser_auth_debug_snapshot(
                snapshot,
                session,
                tracked_open=tracked_open,
                profile_busy=profile_busy,
            )
            snapshot["login_window_open"] = tracked_open
            snapshot["login_opened_at"] = str((session or {}).get("opened_at") or "")
        return {
            "platforms": snapshots,
            "diagnostics": build_browser_runtime_diagnostics(snapshots),
        }

    def _open_browser_auth_login(
        self,
        platform_name: str,
        *,
        profile_id: str = "",
        fresh_profile: bool = False,
    ) -> dict[str, Any]:
        normalized = normalize_browser_platform_name(platform_name)
        if normalized not in BROWSER_PLATFORM_IDS:
            return {"ok": False, "message": f"当前平台暂不支持浏览器账号管理：{platform_name}"}

        self._close_runtime_platform_for_auth(normalized, reason="准备打开登录窗口")
        self._close_browser_auth_session(normalized, reason="准备打开新的登录窗口")

        try:
            if fresh_profile:
                auth_snapshot = create_fresh_browser_auth_profile(normalized)
            elif str(profile_id or "").strip():
                auth_snapshot = activate_browser_auth_profile(normalized, str(profile_id or "").strip())
            else:
                auth_snapshot = get_browser_auth_snapshot([normalized])[normalized]

            active_profile_path = self._browser_auth_profile_path(auth_snapshot)
            if active_profile_path and self._is_browser_profile_in_use(active_profile_path):
                self._activate_existing_browser_window()
                return {
                    "ok": True,
                    "message": "检测到这个账号环境已被浏览器进程占用，已尝试切到前台。若前台没有看到窗口，可点“关闭窗口”清理残留后再重新打开登录页。",
                    "browser_auth": self.get_browser_auth(),
                }

            external_pid = self._launch_external_browser_auth_window(normalized, active_profile_path)
            self._mark_external_browser_auth_session(
                normalized,
                profile_id=auth_snapshot.get("active_profile_id", ""),
                profile_path=active_profile_path,
                pid=external_pid,
            )
            payload = self.get_browser_auth()
            return {
                "ok": True,
                "message": "登录窗口已使用正式版 Chrome 打开。当前浏览器资料会自动写入这个账号环境，登录完成后可直接关闭窗口。",
                "browser_auth": payload,
            }
        except Exception as exc:
            self._close_browser_auth_session(normalized, reason="启动失败后清理")
            current_snapshot = get_browser_auth_snapshot([normalized]).get(normalized, {})
            active_profile_path = self._browser_auth_profile_path(current_snapshot)
            if (
                self._looks_like_existing_browser_session_error(exc)
                and active_profile_path
                and self._is_browser_profile_in_use(active_profile_path)
            ):
                self._activate_existing_browser_window()
                return {
                    "ok": True,
                    "message": "检测到该账号环境已被现有浏览器会话占用，已尝试切到前台。若前台没有看到窗口，可点“关闭窗口”清理残留后再重新打开登录页。",
                    "browser_auth": self.get_browser_auth(),
                }
            return {"ok": False, "message": f"打开登录窗口失败：{exc}", "browser_auth": self.get_browser_auth()}

    def browser_auth_action(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self._prune_browser_auth_sessions()
        normalized_payload = payload or {}
        action = str(normalized_payload.get("action") or "").strip()
        platform_name = str(normalized_payload.get("platform") or "").strip()
        profile_id = str(normalized_payload.get("profile_id") or "").strip()
        normalized = normalize_browser_platform_name(platform_name)
        if not normalized:
            return {"ok": False, "message": "请选择平台", "browser_auth": self.get_browser_auth()}

        if action == "open":
            return self._open_browser_auth_login(normalized, profile_id=profile_id, fresh_profile=False)
        if action == "replace":
            self._close_runtime_platform_for_auth(normalized, reason="准备更换账号")
            self._close_browser_auth_session(normalized, reason="准备更换账号")
            try:
                reset_browser_auth_environment(normalized)
            except Exception as exc:
                return {"ok": False, "message": f"重建平台浏览器环境失败：{exc}", "browser_auth": self.get_browser_auth()}
            result = self._open_browser_auth_login(normalized, profile_id=profile_id, fresh_profile=False)
            if result.get("ok"):
                result["message"] = "该平台的本地浏览器环境已重建，请在打开的浏览器里重新登录新账号。"
            return result
        if action == "confirm":
            session = self._browser_auth_state.get(normalized)
            opened_at = str((session or {}).get("opened_at") or "").strip()
            profile_path = str((session or {}).get("profile_path") or "").strip()
            external_in_use = bool((session or {}).get("external_in_use"))
            if external_in_use and profile_path:
                self._detach_browser_auth_session(normalized, reason="用户确认已登录")
                self._schedule_browser_auth_profile_close(
                    profile_path,
                    delay_seconds=12.0,
                    reason=f"{normalized} 用户确认已登录",
                    graceful_timeout=12.0,
                    force=True,
                )
            else:
                try:
                    mark_browser_auth_profile_authenticated(normalized, True)
                except Exception as exc:
                    return {"ok": False, "message": f"保存登录状态失败：{exc}", "browser_auth": self.get_browser_auth()}
                self._schedule_browser_auth_session_close(
                    normalized,
                    expected_opened_at=opened_at,
                    delay_seconds=3.0,
                    reason="用户确认已登录",
                    graceful_timeout=8.0,
                    force=False,
                )
                return {
                    "ok": True,
                    "message": "已按手动确认为登录成功。",
                    "browser_auth": self.get_browser_auth(),
                }

            try:
                mark_browser_auth_profile_authenticated(normalized, True)
            except Exception as exc:
                return {"ok": False, "message": f"保存登录状态失败：{exc}", "browser_auth": self.get_browser_auth()}
            return {
                "ok": True,
                "message": "已按手动确认为登录成功，后台会尝试收掉登录窗口。",
                "browser_auth": self.get_browser_auth(),
            }
        if action == "close":
            current_snapshot = get_browser_auth_snapshot([normalized]).get(normalized, {})
            active_profile = current_snapshot.get("active_profile") or {}
            profile_path = str(active_profile.get("absolute_path") or "").strip()
            closed_any = False
            session = self._browser_auth_state.get(normalized)
            if session:
                self._close_browser_auth_session(
                    normalized,
                    reason="用户手动关闭登录窗口",
                    graceful_timeout=8.0,
                    force=True,
                )
                closed_any = True
            else:
                self._detach_browser_auth_session(normalized, reason="用户手动关闭")
            if profile_path and self._is_browser_profile_in_use(profile_path):
                closed_any = self._terminate_browser_profile_processes(
                    profile_path,
                    graceful_timeout=8.0,
                    force=True,
                ) or closed_any
                time.sleep(0.8)
            return {
                "ok": True,
                "message": (
                    "已尝试关闭该平台登录窗口及其后台进程。"
                    if closed_any
                    else "当前没有检测到可关闭的登录窗口进程。"
                ),
                "browser_auth": self.get_browser_auth(),
            }
        if action == "logout":
            self._close_runtime_platform_for_auth(normalized, reason="准备退出账号")
            self._close_browser_auth_session(normalized, reason="准备退出账号")
            try:
                reset_browser_auth_environment(normalized)
            except Exception as exc:
                return {"ok": False, "message": f"退出登录失败：{exc}", "browser_auth": self.get_browser_auth()}
            return {
                "ok": True,
                "message": "当前平台的本地浏览器环境已重建，下次打开时会像新的浏览器资料夹一样重新登录。",
                "browser_auth": self.get_browser_auth(),
            }
        if action == "activate":
            self._close_runtime_platform_for_auth(normalized, reason="切换账号环境")
            self._close_browser_auth_session(normalized, reason="切换账号环境")
            try:
                activate_browser_auth_profile(normalized, profile_id)
            except Exception as exc:
                return {"ok": False, "message": f"切换账号环境失败：{exc}", "browser_auth": self.get_browser_auth()}
            return {
                "ok": True,
                "message": "已切换当前账号环境，后续抓取会使用这个资料夹。",
                "browser_auth": self.get_browser_auth(),
            }
        return {"ok": False, "message": f"不支持的账号动作：{action}", "browser_auth": self.get_browser_auth()}

    def _ensure_context_snapshots(
        self,
        config: dict | None = None,
        *,
        force: bool = False,
        refresh_stale: bool = True,
        auto_locate_daily: bool = False,
    ) -> tuple[dict, dict[str, Any]]:
        with self._context_snapshot_lock:
            current = config or self.load_config()
            result = ensure_context_snapshots(
                current,
                config_path=str(self.config_path),
                force=force,
                refresh_stale=refresh_stale,
                auto_locate_daily=auto_locate_daily,
            )
            return result.get("config", current), result

    def schedule_context_snapshot_startup_refresh(self) -> None:
        def _runner() -> None:
            try:
                self._ensure_context_snapshots(
                    force=False,
                    refresh_stale=True,
                    auto_locate_daily=True,
                )
            except Exception as exc:
                print(f"[WebBackend] 天气与节日后台刷新失败: {exc}")

        threading.Thread(
            target=_runner,
            name="context-snapshot-startup-refresh",
            daemon=True,
        ).start()

    def _prune_test_failure_notices(self) -> None:
        now = local_now()
        expired = [
            task_id
            for task_id, notice in self._test_failure_notices.items()
            if str((notice or {}).get("expiresAt") or "").strip()
            and str((notice or {}).get("expiresAt") or "").strip() <= now.isoformat(timespec="seconds")
        ]
        for task_id in expired:
            self._test_failure_notices.pop(task_id, None)

    def _set_test_failure_notice(self, task_id: str, message: str, *, ttl_minutes: int = 30, run_id: str = "") -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        now = local_now()
        expires_at = now + timedelta(minutes=max(1, int(ttl_minutes or 30)))
        self._test_failure_notices[task_id] = {
            "message": str(message or "测试失败").strip() or "测试失败",
            "updatedAt": now.isoformat(timespec="seconds"),
            "expiresAt": expires_at.isoformat(timespec="seconds"),
            "runId": str(run_id or "").strip(),
        }

    def _clear_test_failure_notice(self, task_id: str) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        self._test_failure_notices.pop(task_id, None)

    def _get_test_failure_notice(self, task_id: str) -> dict[str, Any] | None:
        self._prune_test_failure_notices()
        notice = self._test_failure_notices.get(str(task_id or "").strip())
        return dict(notice) if isinstance(notice, dict) else None

    def get_debug_paths(self) -> dict[str, Any]:
        return {
            "ok": True,
            "paths": {
                "appRoot": str(get_app_root()),
                "dataRoot": str(get_data_root()),
                "configPath": str(self.config_path),
                "articlesPath": str(get_articles_file_path()),
                "frontendDist": str(self.frontend_dist),
            },
        }

    def _save_runtime_config(self, config: dict[str, Any]) -> None:
        self.save_config(config)
        self._invalidate_tasks_full_cache()
        self._invalidate_article_cache()

    def _apply_sync_bundle(self, bundle: dict[str, Any], *, mode: str = "merge") -> dict[str, Any]:
        with self._lock:
            current_config = self.load_config()
            result = apply_sync_bundle(
                self.config_path,
                current_config,
                {"bundle": bundle, "mode": mode},
                mode=mode,
            )
        next_config = result.get("config", current_config)
        self._sync_recognition_mode(next_config)
        self._refresh_monitoring_runtime(restart_scheduler=False)
        return result

    def export_sync_bundle(self, include_secrets: bool = False) -> dict[str, Any]:
        config = self.load_config()
        bundle = build_sync_bundle(config, include_secrets=include_secrets)
        bundle["source"] = {
            "appName": APP_NAME,
            "version": get_version_payload(),
            "dataRoot": str(get_data_root()),
        }
        article_store_snapshot = export_article_store_bundle()
        return {
            "ok": True,
            "bundle": bundle,
            "summary": {
                "taskCount": len(bundle.get("data", {}).get("tasks", []) or []),
                "todoCount": len(bundle.get("data", {}).get("todos", []) or []),
                "articleCount": len(article_store_snapshot.get("articles", []) or []),
                "excludedArticleUrlCount": len(article_store_snapshot.get("excluded_article_urls", {}) or {}),
                "includeSecrets": bool(include_secrets),
            },
        }

    def import_sync_bundle(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        mode = str(request_payload.get("mode", "merge") or "merge").strip().lower()
        raw_bundle = request_payload.get("bundle")
        bundle_payload = raw_bundle if isinstance(raw_bundle, dict) else request_payload
        return self._apply_sync_bundle(bundle_payload if isinstance(bundle_payload, dict) else {}, mode=mode)

    def get_cloud_sync_status(self) -> dict[str, Any]:
        return {"ok": True, "cloud_sync": self._cloud_sync_manager.get_status()}

    def _cloud_runtime_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = str(command or "").strip()
        if normalized not in DAEMON_SUPPORTED_COMMANDS or normalized == "cloud.logout":
            return self._ensure_cloud_runtime_support().handle_command(command, payload)
        client = self._ensure_cloud_command_client()
        result = client.send_command(normalized, payload)
        if isinstance(result, dict) and bool(result.get("daemon_unavailable")):
            self._degrade_cloud_command_transport(str(result.get("message") or "云同步 daemon 不可用"))
            return self._ensure_cloud_runtime_support().handle_command(normalized, payload)
        if isinstance(result, dict) and bool(result.get("unsupported_by_daemon")):
            return self._ensure_cloud_runtime_support().handle_command(normalized, payload)
        if normalized in {"cloud.status", "cloud.current_status", "cloud.status_from_session", "cloud.validate_session"}:
            auto_sync_getter = getattr(getattr(self, "_cloud_platform_auto_sync", None), "get_status", None)
            if callable(auto_sync_getter) and isinstance(result, dict):
                cloud = result.get("cloud") if isinstance(result.get("cloud"), dict) else None
                if isinstance(cloud, dict):
                    merged = dict(result)
                    merged_cloud = dict(cloud)
                    merged_cloud["autoSync"] = auto_sync_getter()
                    merged["cloud"] = merged_cloud
                    return merged
        if isinstance(result, dict):
            return result
        return {"ok": False, "message": "云同步命令返回无效响应"}

    def _cloud_command_transport_status(self) -> dict[str, Any]:
        daemon = getattr(self, "_cloud_command_daemon", None)
        server = getattr(self, "_cloud_command_server", None)
        client = getattr(self, "_cloud_command_client", None)
        socket_path = getattr(self, "_cloud_command_socket_path", None)
        mode = str(getattr(self, "_cloud_command_transport_mode", "") or "")
        if not mode:
            if daemon is not None:
                mode = "child_daemon"
            elif server is not None:
                mode = "in_process_socket"
            elif client is not None:
                mode = "in_process_direct"
            else:
                mode = "not_started"
        process = getattr(daemon, "process", None)
        daemon_process_alive = bool(callable(getattr(process, "is_alive", None)) and process.is_alive())
        socket_ping: dict[str, Any] = {
            "attempted": False,
            "ok": None,
            "elapsed_ms": None,
            "daemon": False,
            "pid": None,
            "message": "",
        }
        if mode == "child_daemon" and socket_path:
            socket_ping["attempted"] = True
            started_at = time.monotonic()
            ping_result: dict[str, Any]
            try:
                ping_result = UnixSocketCloudSyncCommandClient(socket_path, timeout_seconds=0.25).send_command(
                    "cloud.daemon.ping"
                )
            except Exception as exc:
                ping_result = {"ok": False, "message": str(exc)}
            socket_ping["elapsed_ms"] = int((time.monotonic() - started_at) * 1000)
            socket_ping["ok"] = bool(ping_result.get("ok"))
            socket_ping["daemon"] = bool(ping_result.get("daemon"))
            socket_ping["pid"] = ping_result.get("pid") if isinstance(ping_result.get("pid"), int) else None
            if not bool(socket_ping["ok"]):
                socket_ping["message"] = str(ping_result.get("message") or "daemon ping failed")
        if mode == "child_daemon":
            responsive = bool(socket_ping.get("ok"))
        elif mode == "in_process_socket":
            responsive = server is not None and client is not None
        elif mode == "in_process_direct":
            responsive = client is not None
        else:
            responsive = False
        return {
            "mode": mode,
            "socket_path": str(socket_path or ""),
            "client_active": client is not None,
            "daemon_active": daemon is not None,
            "daemon_process_alive": daemon_process_alive,
            "in_process_server_active": server is not None,
            "responsive": responsive,
            "socket_ping": socket_ping,
            "last_error": str(getattr(self, "_cloud_command_transport_error", "") or ""),
        }

    def _cloud_runtime_payload_command(self, command: str, payload: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        result = self._cloud_runtime_command(command, payload)
        if not isinstance(result, dict):
            return False, None, ""
        return bool(result.get("ok")), result.get("payload"), str(result.get("message") or "")

    def _validate_cloud_session_if_needed(self, *, force: bool = False) -> None:
        self._cloud_runtime_command("cloud.validate_session", {"force": force})

    def get_cloud_status(self) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.status")

    def get_cloud_outbox_diagnostics(self) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.outbox_diagnostics")

    def get_cloud_sync_health(self) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.sync_health")

    def get_cloud_sync_health_deep(self) -> dict[str, Any]:
        result = self._cloud_runtime_command("cloud.sync_health", {"includeCloudObjectStorage": True})
        if isinstance(result, dict) and isinstance(result.get("sync_health"), dict):
            sync_health = dict(result["sync_health"])
            sync_health["cloud_command_transport"] = self._cloud_command_transport_status()
            result = dict(result)
            result["sync_health"] = sync_health
        return result

    def get_cloud_state_delta_diagnostics(self) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.state_delta_diagnostics")

    def _current_cloud_status(self) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.current_status")

    def _cloud_status_from_session(self, session: dict[str, Any] | None) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.status_from_session", {"session": session})

    def list_cloud_admin_tasks(self) -> dict[str, Any]:
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "tasks": [], "cloud": self.get_cloud_status().get("cloud")}
        local_snapshots = self._cloud_task_ensure_snapshots()
        ok, payload, message = self._cloud_runtime_payload_command(
            "cloud.list_admin_tasks_with_local_sync",
            {"local_snapshots": local_snapshots},
        )
        if not ok:
            return {"ok": False, "message": message or "云端任务获取失败", "tasks": [], "cloud": self.get_cloud_status().get("cloud")}
        if isinstance(payload, dict):
            local_updates = payload.get("local_updates") if isinstance(payload.get("local_updates"), list) else []
            if local_updates:
                self._apply_cloud_task_local_updates(local_updates)
            tasks = payload.get("tasks")
        else:
            tasks = []
        return {"ok": True, "message": "云端任务已刷新", "tasks": tasks if isinstance(tasks, list) else [], "cloud": self.get_cloud_status().get("cloud")}

    def _cloud_task_ensure_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            config = self.load_config()
            tasks = list(config.get("tasks", []) or [])
        snapshots: list[dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if bool(task.get("delete_pending")):
                continue
            if str(task.get("cloud_access_level") or "").strip().lower() == "revoked":
                continue
            local_task_id = str(task.get("task_id") or derive_task_id(task)).strip()
            if not local_task_id:
                continue
            snapshots.append({
                "local_task_id": local_task_id,
                "cloud_task_id": _safe_int(task.get("cloud_task_id") or task.get("cloudTaskId"), 0),
                "task_key": self._cloud_task_key_for_local_task(task, local_task_id),
                "payload": {
                    "name": str(task.get("name") or task.get("brand") or local_task_id).strip(),
                    "brand": str(task.get("brand") or task.get("name") or local_task_id).strip(),
                    "config_json": self._build_cloud_config_from_local_task(task),
                    "enabled": bool(task.get("enabled", True)),
                },
                "operator_user_id": _safe_int(task.get("cloud_assigned_operator_user_id"), 0),
            })
        return snapshots

    def _ensure_local_admin_tasks_in_cloud(
        self,
        client: SurfacedCloudClient,
        token: str,
        local_snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session = CloudSessionStore().load()
        default_operator_user_id = self._default_cloud_operator_user_id(session)
        remote_tasks = client.list_admin_tasks(token)
        if not isinstance(remote_tasks, list):
            remote_tasks = []
        remote_by_id = {
            _safe_int(task.get("id") if isinstance(task, dict) else 0, 0): task
            for task in remote_tasks
            if isinstance(task, dict) and _safe_int(task.get("id"), 0) > 0
        }
        remote_by_key = {
            str(task.get("task_key") or "").strip(): task
            for task in remote_tasks
            if isinstance(task, dict) and str(task.get("task_key") or "").strip()
        }
        local_updates: list[dict[str, Any]] = []
        changed_remote = False
        for snapshot in local_snapshots:
            local_task_id = str(snapshot.get("local_task_id") or "").strip()
            task_key = str(snapshot.get("task_key") or "").strip()
            if not local_task_id or not task_key:
                continue
            cloud_task_id = _safe_int(snapshot.get("cloud_task_id"), 0)
            saved_task = remote_by_id.get(cloud_task_id) if cloud_task_id > 0 else None
            if saved_task is None:
                saved_task = remote_by_key.get(task_key)
            if saved_task is None:
                payload = dict(snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {})
                saved_task = client.create_admin_task(token, {"task_key": task_key, **payload})
                changed_remote = True
                saved_task_id = _safe_int(saved_task.get("id") if isinstance(saved_task, dict) else 0, 0)
                operator_user_id = _safe_int(snapshot.get("operator_user_id"), 0) or default_operator_user_id
                if saved_task_id > 0 and operator_user_id > 0:
                    client.assign_admin_task_member(
                        token,
                        saved_task_id,
                        user_id=operator_user_id,
                        access_level="operate",
                        note="同步本地品牌任务",
                    )
                    if isinstance(saved_task, dict):
                        saved_task = dict(saved_task)
                        saved_task["assigned_operator_user_id"] = operator_user_id
            if not isinstance(saved_task, dict):
                continue
            saved_task_id = _safe_int(saved_task.get("id"), 0)
            if saved_task_id <= 0:
                continue
            operator_assignment_changed = False
            desired_operator_user_id = _safe_int(snapshot.get("operator_user_id"), 0) or default_operator_user_id
            current_operator_user_id = _safe_int(saved_task.get("assigned_operator_user_id"), 0)
            if desired_operator_user_id > 0 and current_operator_user_id != desired_operator_user_id:
                client.assign_admin_task_member(
                    token,
                    saved_task_id,
                    user_id=desired_operator_user_id,
                    access_level="operate",
                    note="同步本地品牌任务",
                )
                changed_remote = True
                operator_assignment_changed = True
                try:
                    refreshed_tasks = client.list_admin_tasks(token)
                    if isinstance(refreshed_tasks, list):
                        remote_tasks = refreshed_tasks
                        remote_by_id = {
                            _safe_int(task.get("id") if isinstance(task, dict) else 0, 0): task
                            for task in remote_tasks
                            if isinstance(task, dict) and _safe_int(task.get("id"), 0) > 0
                        }
                        saved_task = remote_by_id.get(saved_task_id, saved_task)
                except Exception:
                    pass
            if (
                cloud_task_id != saved_task_id
                or task_key != str(saved_task.get("task_key") or "").strip()
                or operator_assignment_changed
            ):
                local_updates.append({
                    "local_task_id": local_task_id,
                    "task": saved_task,
                })
        if changed_remote:
            remote_tasks = client.list_admin_tasks(token)
            if not isinstance(remote_tasks, list):
                remote_tasks = []
        return {"tasks": remote_tasks, "local_updates": local_updates}

    def _apply_cloud_task_local_updates(self, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return
        updates_by_local_id = {
            str(item.get("local_task_id") or "").strip(): item.get("task")
            for item in updates
            if isinstance(item, dict) and isinstance(item.get("task"), dict)
        }
        updates_by_local_id = {key: value for key, value in updates_by_local_id.items() if key}
        if not updates_by_local_id:
            return
        with self._lock:
            config = self.load_config()
            tasks = config.get("tasks", []) or []
            changed = False
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                local_task_id = str(task.get("task_id") or derive_task_id(task)).strip()
                saved_task = updates_by_local_id.get(local_task_id)
                if not isinstance(saved_task, dict):
                    continue
                before = dict(task)
                task["cloud_task_id"] = _safe_int(saved_task.get("id"), _safe_int(task.get("cloud_task_id"), 0))
                task["cloud_task_key"] = str(saved_task.get("task_key") or task.get("cloud_task_key") or "").strip()
                task["cloud_workspace_id"] = _safe_int(saved_task.get("workspace_id"), _safe_int(task.get("cloud_workspace_id"), 0))
                task["cloud_config_version"] = _safe_int(saved_task.get("config_version"), _safe_int(task.get("cloud_config_version"), 1))
                task["cloud_access_level"] = "admin"
                task["cloud_assigned_operator_user_id"] = _safe_int(
                    saved_task.get("assigned_operator_user_id"),
                    _safe_int(task.get("cloud_assigned_operator_user_id"), 0),
                )
                task["cloud_assigned_operator_username"] = str(
                    saved_task.get("assigned_operator_username") or task.get("cloud_assigned_operator_username") or ""
                ).strip()
                task["cloud_synced_at"] = _local_iso_seconds()
                if task != before:
                    changed = True
            if changed:
                config["tasks"] = tasks
                self.save_config(config)
                self._invalidate_tasks_full_cache()
                self._invalidate_article_cache()

    @staticmethod
    def _default_cloud_operator_user_id(session: dict[str, Any] | None) -> int:
        session_payload = session if isinstance(session, dict) else {}
        user = session_payload.get("user") if isinstance(session_payload.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return 0
        return _safe_int(user.get("id"), 0)

    def _resolve_cloud_operator_user_id(self, session: dict[str, Any] | None, requested_user_id: int) -> int:
        operator_user_id = _safe_int(requested_user_id, 0)
        if operator_user_id > 0:
            return operator_user_id
        return self._default_cloud_operator_user_id(session)

    def list_cloud_admin_users(self) -> dict[str, Any]:
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "users": [], "cloud": self.get_cloud_status().get("cloud")}
        ok, users, message = self._cloud_runtime_payload_command("cloud.list_admin_users")
        if not ok:
            return {"ok": False, "message": message or "云端账号获取失败", "users": [], "cloud": self.get_cloud_status().get("cloud")}
        return {"ok": True, "message": "云端账号已刷新", "users": users if isinstance(users, list) else [], "cloud": self.get_cloud_status().get("cloud")}

    def list_cloud_article_classification_jobs(self) -> dict[str, Any]:
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "jobs": [], "cloud": self.get_cloud_status().get("cloud")}
        ok, jobs, message = self._cloud_runtime_payload_command(
            "cloud.list_admin_article_classification_jobs",
            {"status": "unresolved", "limit": 200},
        )
        if not ok:
            return {"ok": False, "message": message or "未归类文章获取失败", "jobs": [], "cloud": self.get_cloud_status().get("cloud")}
        return {
            "ok": True,
            "message": "未归类文章已刷新",
            "jobs": jobs if isinstance(jobs, list) else [],
            "cloud": self.get_cloud_status().get("cloud"),
        }

    def _refresh_cloud_articles_after_classification_change(self) -> dict[str, Any]:
        try:
            with self._lock:
                config = self.load_config()
                summary = pull_cloud_articles_into_store(config, force_full=False)
                if summary.get("ok") and int(summary.get("state_updated") or 0):
                    self.save_config(config)
            if summary.get("ok") and (
                int(summary.get("imported") or 0)
                or int(summary.get("updated") or 0)
                or int(summary.get("pruned") or 0)
                or int(summary.get("pruned_links") or 0)
            ):
                self._invalidate_article_cache()
            return summary
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def resolve_cloud_article_classification_job(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        job_id = _safe_int(request_payload.get("job_id") or request_payload.get("jobId"), 0)
        task_id = _safe_int(request_payload.get("task_id") or request_payload.get("taskId"), 0)
        if job_id <= 0 or task_id <= 0:
            return {"ok": False, "message": "请选择文章和归属品牌", "cloud": self.get_cloud_status().get("cloud")}
        reason = str(request_payload.get("reason") or "管理员归类未归类文章").strip()
        ok, job, message = self._cloud_runtime_payload_command(
            "cloud.resolve_admin_article_classification_job",
            {"job_id": job_id, "task_id": task_id, "reason": reason},
        )
        if not ok:
            return {"ok": False, "message": message or "文章归类失败", "cloud": self.get_cloud_status().get("cloud")}
        article_summary = self._refresh_cloud_articles_after_classification_change()
        return {
            "ok": True,
            "message": "文章已归类",
            "job": job if isinstance(job, dict) else {},
            "articles": article_summary,
            "cloud": self.get_cloud_status().get("cloud"),
        }

    def ignore_cloud_article_classification_job(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        job_id = _safe_int(request_payload.get("job_id") or request_payload.get("jobId"), 0)
        if job_id <= 0:
            return {"ok": False, "message": "缺少未归类文章 ID", "cloud": self.get_cloud_status().get("cloud")}
        reason = str(request_payload.get("reason") or "管理员忽略未归类文章").strip()
        ok, job, message = self._cloud_runtime_payload_command(
            "cloud.ignore_admin_article_classification_job",
            {"job_id": job_id, "reason": reason},
        )
        if not ok:
            return {"ok": False, "message": message or "文章忽略失败", "cloud": self.get_cloud_status().get("cloud")}
        return {
            "ok": True,
            "message": "文章已忽略",
            "job": job if isinstance(job, dict) else {},
            "cloud": self.get_cloud_status().get("cloud"),
        }

    def create_cloud_admin_user(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        username = str(request_payload.get("username") or "").strip()
        password = str(request_payload.get("password") or "")
        role = str(request_payload.get("role") or "").strip()
        display_name = str(request_payload.get("display_name") or request_payload.get("displayName") or "").strip()
        email = str(request_payload.get("email") or "").strip()
        birthday = str(request_payload.get("birthday") or "").strip()
        hire_date = str(request_payload.get("hire_date") or request_payload.get("hireDate") or "").strip()
        view_all_tasks = bool(request_payload.get("view_all_tasks") or request_payload.get("viewAllTasks"))
        visible_task_ids = request_payload.get("visible_task_ids") or request_payload.get("visibleTaskIds") or []
        if not username or not password:
            return {"ok": False, "message": "请填写账号和密码", "cloud": self.get_cloud_status().get("cloud")}
        if len(password) < 8:
            return {"ok": False, "message": "密码至少需要 8 位", "cloud": self.get_cloud_status().get("cloud")}
        if role not in {"operator", "viewer"}:
            return {"ok": False, "message": "账号类型只能是运营账号或浏览账号", "cloud": self.get_cloud_status().get("cloud")}

        create_payload: dict[str, Any] = {
            "username": username,
            "password": password,
            "role": role,
            "display_name": display_name or None,
            "email": email or None,
            "birthday": birthday or None,
            "hire_date": hire_date or None,
        }
        if role == "viewer":
            create_payload["view_all_tasks"] = view_all_tasks
            create_payload["visible_task_ids"] = _normalize_cloud_task_id_list(visible_task_ids)
        ok, created_user, message = self._cloud_runtime_payload_command("cloud.create_admin_user", {"payload": create_payload})
        if not ok:
            return {"ok": False, "message": message or "云端账号创建失败", "cloud": self.get_cloud_status().get("cloud")}
        return {
            "ok": True,
            "message": "云端账号已创建",
            "user": created_user if isinstance(created_user, dict) else {},
            "cloud": self.get_cloud_status().get("cloud"),
        }

    def update_cloud_admin_user(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        user_id = _safe_int(request_payload.get("user_id") or request_payload.get("userId"), 0)
        if user_id <= 0:
            return {"ok": False, "message": "缺少云端账号 ID", "cloud": self.get_cloud_status().get("cloud")}

        update_payload: dict[str, Any] = {}
        if "username" in request_payload:
            username = str(request_payload.get("username") or "").strip()
            if not username:
                return {"ok": False, "message": "请填写账号名", "cloud": self.get_cloud_status().get("cloud")}
            update_payload["username"] = username
        if "password" in request_payload and str(request_payload.get("password") or ""):
            password = str(request_payload.get("password") or "")
            if len(password) < 8:
                return {"ok": False, "message": "密码至少需要 8 位", "cloud": self.get_cloud_status().get("cloud")}
            update_payload["password"] = password
        if "display_name" in request_payload or "displayName" in request_payload:
            update_payload["display_name"] = str(request_payload.get("display_name") or request_payload.get("displayName") or "").strip() or None
        if "email" in request_payload:
            email = str(request_payload.get("email") or "").strip()
            update_payload["email"] = email or None
        if "birthday" in request_payload:
            update_payload["birthday"] = str(request_payload.get("birthday") or "").strip() or None
        if "hire_date" in request_payload or "hireDate" in request_payload:
            update_payload["hire_date"] = str(request_payload.get("hire_date") or request_payload.get("hireDate") or "").strip() or None
        if "view_all_tasks" in request_payload or "viewAllTasks" in request_payload:
            update_payload["view_all_tasks"] = bool(request_payload.get("view_all_tasks") or request_payload.get("viewAllTasks"))
        if "visible_task_ids" in request_payload or "visibleTaskIds" in request_payload:
            update_payload["visible_task_ids"] = _normalize_cloud_task_id_list(
                request_payload.get("visible_task_ids") or request_payload.get("visibleTaskIds") or []
            )
        if "enabled" in request_payload:
            update_payload["enabled"] = bool(request_payload.get("enabled"))
        if not update_payload:
            return {"ok": False, "message": "没有可保存的账号改动", "cloud": self.get_cloud_status().get("cloud")}

        ok, updated_user, message = self._cloud_runtime_payload_command(
            "cloud.update_admin_user",
            {"user_id": user_id, "payload": update_payload},
        )
        if not ok:
            return {"ok": False, "message": message or "云端账号保存失败", "cloud": self.get_cloud_status().get("cloud")}
        return {
            "ok": True,
            "message": "云端账号已保存",
            "user": updated_user if isinstance(updated_user, dict) else {},
            "cloud": self.get_cloud_status().get("cloud"),
        }

    def delete_cloud_admin_user(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        user_id = _safe_int(request_payload.get("user_id") or request_payload.get("userId"), 0)
        if user_id <= 0:
            return {"ok": False, "message": "缺少云端账号 ID", "cloud": self.get_cloud_status().get("cloud")}
        ok, _payload, message = self._cloud_runtime_payload_command("cloud.delete_admin_user", {"user_id": user_id})
        if not ok:
            return {"ok": False, "message": message or "云端账号删除失败", "cloud": self.get_cloud_status().get("cloud")}
        return {"ok": True, "message": "云端账号已删除", "cloud": self.get_cloud_status().get("cloud")}

    def login_cloud(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        base_url = str(request_payload.get("base_url") or request_payload.get("baseUrl") or "").strip()
        username = str(request_payload.get("username") or "").strip()
        password = str(request_payload.get("password") or "")
        device_id = str(request_payload.get("device_id") or request_payload.get("deviceId") or "").strip()
        app_version = str(request_payload.get("app_version") or request_payload.get("appVersion") or "").strip()
        if not base_url or not username or not password:
            return {"ok": False, "message": "请填写云端地址、用户名和密码", "cloud": self.get_cloud_status().get("cloud")}
        if not device_id:
            device_id = f"surfaced-local-{uuid4().hex[:12]}"
        if not app_version:
            app_version = str(get_version_payload().get("version") or get_http_server_version() or "local")
        client = SurfacedCloudClient(base_url)
        try:
            token_pair = client.login(
                username=username,
                password=password,
                device_id=device_id,
                app_version=app_version,
            )
        except CloudClientError as exc:
            return {"ok": False, "message": str(exc), "cloud": self.get_cloud_status().get("cloud")}
        saved_session = self._login_cloud_account_space(
            base_url=base_url,
            token_pair=token_pair,
        )
        return {"ok": True, "message": "云端登录成功", "cloud": self._cloud_status_from_session(saved_session).get("cloud")}

    def register_cloud_admin(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        base_url = str(request_payload.get("base_url") or request_payload.get("baseUrl") or "").strip()
        email = str(request_payload.get("email") or request_payload.get("username") or "").strip().lower()
        password = str(request_payload.get("password") or "")
        workspace_name = str(request_payload.get("workspace_name") or request_payload.get("workspaceName") or "").strip()
        display_name = str(request_payload.get("display_name") or request_payload.get("displayName") or "").strip()
        if not base_url or not email or not password:
            return {"ok": False, "message": "请填写云端地址、邮箱和密码", "cloud": self.get_cloud_status().get("cloud")}
        if len(password) < 8:
            return {"ok": False, "message": "密码至少需要 8 位", "cloud": self.get_cloud_status().get("cloud")}
        if not workspace_name:
            workspace_name = display_name or email.split("@", 1)[0] or "Surfaced Workspace"
        client = SurfacedCloudClient(base_url)
        try:
            registered_user = client.register_admin(
                email=email,
                password=password,
                workspace_name=workspace_name,
                display_name=display_name,
            )
        except CloudClientError as exc:
            return {"ok": False, "message": str(exc), "cloud": self.get_cloud_status().get("cloud")}
        if not bool((registered_user or {}).get("email_verified")):
            return {
                "ok": True,
                "message": "验证码已发送，请查收邮箱",
                "requiresEmailVerification": True,
                "email": email,
                "cloud": self.get_cloud_status().get("cloud"),
            }
        login_result = self.login_cloud({
            "base_url": base_url,
            "username": email,
            "password": password,
        })
        if login_result.get("ok"):
            login_result["message"] = "管理员账号注册成功"
        return login_result

    def _save_cloud_token_pair(self, *, base_url: str, token_pair: dict[str, Any], message: str) -> dict[str, Any]:
        saved_session = self._login_cloud_account_space(
            base_url=base_url,
            token_pair=token_pair,
        )
        return {"ok": True, "message": message, "cloud": self._cloud_status_from_session(saved_session).get("cloud")}

    def verify_cloud_email(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        base_url = str(request_payload.get("base_url") or request_payload.get("baseUrl") or "").strip()
        email = str(request_payload.get("email") or "").strip().lower()
        code = str(request_payload.get("code") or "").strip()
        device_id = str(request_payload.get("device_id") or request_payload.get("deviceId") or "").strip()
        app_version = str(request_payload.get("app_version") or request_payload.get("appVersion") or "").strip()
        if not base_url or not email or not code:
            return {"ok": False, "message": "请填写邮箱验证码", "cloud": self.get_cloud_status().get("cloud")}
        if not device_id:
            device_id = f"surfaced-local-{uuid4().hex[:12]}"
        if not app_version:
            app_version = str(get_version_payload().get("version") or get_http_server_version() or "local")
        try:
            token_pair = SurfacedCloudClient(base_url).verify_email(
                email=email,
                code=code,
                device_id=device_id,
                app_version=app_version,
            )
        except CloudClientError as exc:
            return {"ok": False, "message": str(exc), "cloud": self.get_cloud_status().get("cloud")}
        return self._save_cloud_token_pair(base_url=base_url, token_pair=token_pair, message="邮箱验证成功")

    def resend_cloud_email_code(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        base_url = str(request_payload.get("base_url") or request_payload.get("baseUrl") or "").strip()
        email = str(request_payload.get("email") or "").strip().lower()
        if not base_url or not email:
            return {"ok": False, "message": "请填写邮箱", "cloud": self.get_cloud_status().get("cloud")}
        try:
            SurfacedCloudClient(base_url).resend_email_verification(email=email)
        except CloudClientError as exc:
            return {"ok": False, "message": str(exc), "cloud": self.get_cloud_status().get("cloud")}
        return {"ok": True, "message": "验证码已重新发送", "cloud": self.get_cloud_status().get("cloud")}

    def request_cloud_password_reset(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        base_url = str(request_payload.get("base_url") or request_payload.get("baseUrl") or "").strip()
        email = str(request_payload.get("email") or "").strip().lower()
        if not base_url or not email:
            return {"ok": False, "message": "请填写管理员邮箱", "cloud": self.get_cloud_status().get("cloud")}
        try:
            SurfacedCloudClient(base_url).request_password_reset(email=email)
        except CloudClientError as exc:
            return {"ok": False, "message": str(exc), "cloud": self.get_cloud_status().get("cloud")}
        return {"ok": True, "message": "如果该邮箱已注册，验证码将发送至对应邮箱", "cloud": self.get_cloud_status().get("cloud")}

    def reset_cloud_password(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        base_url = str(request_payload.get("base_url") or request_payload.get("baseUrl") or "").strip()
        email = str(request_payload.get("email") or "").strip().lower()
        code = str(request_payload.get("code") or "").strip()
        password = str(request_payload.get("password") or "")
        if not base_url or not email or not code or not password:
            return {"ok": False, "message": "请填写邮箱、验证码和新密码", "cloud": self.get_cloud_status().get("cloud")}
        if len(password) < 8:
            return {"ok": False, "message": "密码至少需要 8 位", "cloud": self.get_cloud_status().get("cloud")}
        try:
            SurfacedCloudClient(base_url).reset_password(email=email, code=code, password=password)
        except CloudClientError as exc:
            return {"ok": False, "message": str(exc), "cloud": self.get_cloud_status().get("cloud")}
        return {"ok": True, "message": "密码已重置，请使用新密码登录", "cloud": self.get_cloud_status().get("cloud")}

    def logout_cloud(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        del payload
        return self._cloud_runtime_command("cloud.logout")

    def flush_cloud_outbox(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.flush_outbox", payload)

    def pull_cloud_state_delta(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.pull_state_delta", payload)

    def process_cloud_state_delta_inbox(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.process_state_delta_inbox", payload)

    def _recover_cloud_run_history_uploads(self) -> dict[str, Any]:
        return self._cloud_runtime_command("cloud.recover_uploads")

    def pull_cloud_tasks(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        force_full = bool(request_payload.get("force") or request_payload.get("force_full") or request_payload.get("forceFull"))
        task_config_changed = 0
        with self._lock:
            config = self.load_config()
            result = pull_cloud_tasks_into_config(config, force_full=force_full)
            if result.get("ok"):
                summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
                run_summary = summary.get("run_records") if isinstance(summary.get("run_records"), dict) else {}
                status_summary = (
                    summary.get("task_day_status_events")
                    if isinstance(summary.get("task_day_status_events"), dict)
                    else {}
                )
                task_config_changed = (
                    int(summary.get("added") or 0)
                    + int(summary.get("updated") or 0)
                    + int(summary.get("revoked") or 0)
                    + int(summary.get("deleted") or 0)
                    + int(summary.get("deleted_backups") or 0)
                    + int(summary.get("deleted_pending") or 0)
                )
                change_summary = summary.get("changes") if isinstance(summary.get("changes"), dict) else {}
                change_events = [
                    str(item or "").strip()
                    for item in (change_summary.get("events") if isinstance(change_summary.get("events"), list) else [])
                ]
                article_summary = pull_cloud_articles_into_store(
                    config,
                    force_full=force_full or task_config_changed > 0 or EVENT_ARTICLE_CHANGED in change_events,
                )
                summary["articles"] = article_summary
                changed = (
                    task_config_changed
                    + int(summary.get("state_updated") or 0)
                    + int(run_summary.get("cursor_updates") or 0)
                    + int(run_summary.get("backfilled") or 0)
                    + int(status_summary.get("cursor_updates") or 0)
                    + int(article_summary.get("state_updated") or 0)
                )
                if changed:
                    self.save_config(config)
                if (
                    changed
                    or int(run_summary.get("imported") or 0)
                    or int(article_summary.get("imported") or 0)
                    or int(article_summary.get("updated") or 0)
                    or int(article_summary.get("pruned") or 0)
                    or int(article_summary.get("pruned_links") or 0)
                ):
                    self._invalidate_tasks_full_cache()
                    self._invalidate_article_cache()
        if result.get("ok"):
            # Only restart the scheduler when the cloud pull actually changed the
            # task definitions — restarting on a no-op pull would interrupt any
            # query currently running under the browser scraper.
            self._refresh_monitoring_runtime(restart_scheduler=task_config_changed > 0)
            result["cloud"] = self.get_cloud_status().get("cloud")
        return result

    def update_cloud_admin_task(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        task_id = _safe_int(request_payload.get("task_id") or request_payload.get("taskId"), 0)
        if task_id <= 0:
            return {"ok": False, "message": "缺少云端任务 ID", "cloud": self.get_cloud_status().get("cloud")}

        update_payload: dict[str, Any] = {}
        for key in ("name", "brand", "enabled"):
            if key in request_payload:
                update_payload[key] = request_payload.get(key)
        if "config_json" in request_payload:
            config_json = request_payload.get("config_json")
            if not isinstance(config_json, dict):
                return {"ok": False, "message": "任务配置必须是 JSON 对象", "cloud": self.get_cloud_status().get("cloud")}
            update_payload["config_json"] = config_json
        expected_config_version = request_payload.get("expected_config_version") or request_payload.get("expectedConfigVersion")
        if expected_config_version is not None:
            update_payload["expected_config_version"] = _safe_int(expected_config_version, 0)
        if not update_payload:
            return {"ok": False, "message": "没有可保存的云端任务改动", "cloud": self.get_cloud_status().get("cloud")}

        ok, task, message = self._cloud_runtime_payload_command(
            "cloud.update_admin_task",
            {"task_id": task_id, "payload": update_payload},
        )
        if not ok:
            return {"ok": False, "message": message or "云端任务保存失败", "cloud": self.get_cloud_status().get("cloud")}
        return {"ok": True, "message": "云端任务已保存", "task": task if isinstance(task, dict) else {}, "cloud": self.get_cloud_status().get("cloud")}

    def sync_cloud_admin_task(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        local_task_id = str(request_payload.get("local_task_id") or request_payload.get("task_id") or "").strip()
        if not local_task_id:
            return {"ok": False, "message": "缺少本地任务 ID", "cloud": self.get_cloud_status().get("cloud")}
        operator_user_id = self._resolve_cloud_operator_user_id(
            session,
            _safe_int(request_payload.get("operator_user_id") or request_payload.get("operatorUserId"), 0),
        )

        with self._lock:
            config = self.load_config()
            tasks = config.get("tasks", []) or []
            target_index = None
            for index, task in enumerate(tasks):
                if str(task.get("task_id") or derive_task_id(task)).strip() == local_task_id:
                    target_index = index
                    break
            if target_index is None:
                return {"ok": False, "message": f"未找到本地任务 {local_task_id}", "cloud": self.get_cloud_status().get("cloud")}
            local_task = dict(tasks[target_index])

        existing_cloud_task_id = _safe_int(local_task.get("cloud_task_id") or local_task.get("cloudTaskId"), 0)
        cloud_config = self._build_cloud_config_from_local_task(local_task)
        cloud_payload = {
            "name": str(local_task.get("name") or local_task.get("brand") or local_task_id).strip(),
            "brand": str(local_task.get("brand") or local_task.get("name") or local_task_id).strip(),
            "config_json": cloud_config,
            "enabled": bool(local_task.get("enabled", True)),
        }
        if existing_cloud_task_id > 0 and _safe_int(local_task.get("cloud_config_version"), 0) > 0:
            cloud_payload["expected_config_version"] = _safe_int(local_task.get("cloud_config_version"), 0)
        ok, saved_task, message = self._cloud_runtime_payload_command(
            "cloud.sync_admin_task",
            {
                "local_task_id": local_task_id,
                "local_task": local_task,
                "existing_cloud_task_id": existing_cloud_task_id,
                "operator_user_id": operator_user_id,
                "cloud_payload": cloud_payload,
            },
        )
        if not ok:
            return {"ok": False, "message": message or "云端任务同步失败", "cloud": self.get_cloud_status().get("cloud")}
        if not isinstance(saved_task, dict):
            saved_task = {}

        base_snapshot = self._get_cached_task_snapshot(local_task_id)
        local_task_snapshot: dict[str, Any] = {}
        snapshot_config: dict[str, Any] = {}
        with self._lock:
            config = self.load_config()
            tasks = config.get("tasks", []) or []
            for index, task in enumerate(tasks):
                if str(task.get("task_id") or derive_task_id(task)).strip() != local_task_id:
                    continue
                task["cloud_task_id"] = _safe_int(saved_task.get("id"), existing_cloud_task_id)
                task["cloud_task_key"] = str(saved_task.get("task_key") or task.get("cloud_task_key") or "").strip()
                task["cloud_workspace_id"] = _safe_int(saved_task.get("workspace_id"), _safe_int(task.get("cloud_workspace_id"), 0))
                task["cloud_config_version"] = _safe_int(saved_task.get("config_version"), _safe_int(task.get("cloud_config_version"), 1))
                task["cloud_access_level"] = "admin"
                task["cloud_assigned_operator_user_id"] = operator_user_id
                if operator_user_id > 0:
                    task["cloud_assigned_operator_username"] = str(saved_task.get("assigned_operator_username") or task.get("cloud_assigned_operator_username") or "").strip()
                else:
                    task["cloud_assigned_operator_username"] = ""
                task["cloud_synced_at"] = _local_iso_seconds()
                tasks[index] = task
                local_task_snapshot = copy.deepcopy(task)
                break
            config["tasks"] = tasks
            self.save_config(config)
            snapshot_config = copy.deepcopy(config)
            self._invalidate_tasks_full_cache()
            self._invalidate_article_cache()
        self._refresh_monitoring_runtime(restart_scheduler=False)
        return {
            "ok": True,
            "message": "云端任务已同步",
            "task": saved_task,
            "local_task": self._get_light_task_snapshot(
                local_task_id,
                config=snapshot_config,
                task=local_task_snapshot,
                base_snapshot=base_snapshot,
            ),
            "cloud": self.get_cloud_status().get("cloud"),
        }

    def delete_cloud_admin_task(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        cloud_task_id = _safe_int(request_payload.get("task_id") or request_payload.get("taskId"), 0)
        if cloud_task_id <= 0:
            return {"ok": False, "message": "缺少云端任务 ID", "cloud": self.get_cloud_status().get("cloud")}
        ok, _payload, message = self._cloud_runtime_payload_command("cloud.delete_admin_task", {"task_id": cloud_task_id})
        if not ok:
            return {"ok": False, "message": message or "云端任务删除失败", "cloud": self.get_cloud_status().get("cloud")}
        return {"ok": True, "message": "云端任务已软删除", "cloud": self.get_cloud_status().get("cloud")}

    def restore_cloud_admin_task(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        if str(user.get("role") or "").strip() != "admin":
            return {"ok": False, "message": "当前云端账号不是管理员", "cloud": self.get_cloud_status().get("cloud")}
        cloud_task_id = _safe_int(request_payload.get("task_id") or request_payload.get("taskId"), 0)
        if cloud_task_id <= 0:
            return {"ok": False, "message": "缺少云端任务 ID", "cloud": self.get_cloud_status().get("cloud")}
        ok, task, message = self._cloud_runtime_payload_command(
            "cloud.restore_admin_task",
            {"task_id": cloud_task_id},
        )
        if not ok:
            return {"ok": False, "message": message or "云端任务恢复失败", "cloud": self.get_cloud_status().get("cloud")}
        return {
            "ok": True,
            "message": "云端任务已恢复",
            "task": task if isinstance(task, dict) else {},
            "cloud": self.get_cloud_status().get("cloud"),
        }

    @staticmethod
    def _cloud_task_key_for_local_task(task: dict[str, Any], local_task_id: str) -> str:
        existing = str(task.get("cloud_task_key") or "").strip()
        if existing:
            return existing[:128]
        raw = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(local_task_id or "").strip()).strip("-")
        return f"local-{raw or uuid4().hex[:12]}"[:128]

    @staticmethod
    def _build_cloud_config_from_local_task(task: dict[str, Any]) -> dict[str, Any]:
        local_task_keys = [
            "industry_tags",
            "region_tags",
            "weekdays",
            "enabled",
            "inspect",
            "recognition_enabled",
            "recognition_brands",
            "recognition_batch_size",
            "extract_references_enabled",
            "fixed_screenshot_enabled",
            "fixed_screenshot_count",
            "optimization_start_date",
            "optimization_end_date",
            "keywords",
            "platforms",
        ]
        local_task = {key: copy.deepcopy(task.get(key)) for key in local_task_keys if key in task}
        platforms = list(task.get("platforms") or [])
        keywords = copy.deepcopy(task.get("keywords") or [])
        return {
            "platforms": platforms,
            "keywords": keywords,
            "local_task": local_task,
        }

    def get_local_model_status(self) -> dict[str, Any]:
        return {"ok": True, "local_model": get_local_model_manager().get_status()}

    def prepare_local_model(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        config = self.load_config()
        local_cfg = dict(config.get("local_model", {}) or {})
        platform_cfg = dict(_get_platform_config_entry(config, "local_model") or {})
        model = str(
            request_payload.get("model")
            or local_cfg.get("default_model")
            or platform_cfg.get("api_model")
            or (PLATFORM_API_CONFIG.get("local_model") or {}).get("default_model")
            or "gemma4:e2b"
        ).strip()
        manager = get_local_model_manager()
        manager.sync_config(config)
        manager.prepare_in_background(model=model, reason="manual_prepare")
        return {
            "ok": True,
            "queued": True,
            "message": f"已开始准备本地模型：{model}",
            "local_model": manager.get_status(),
        }

    def test_local_model(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        config = self.load_config()
        local_cfg = dict(config.get("local_model", {}) or {})
        platform_cfg = dict(_get_platform_config_entry(config, "local_model") or {})
        model = str(
            request_payload.get("model")
            or local_cfg.get("default_model")
            or platform_cfg.get("api_model")
            or (PLATFORM_API_CONFIG.get("local_model") or {}).get("default_model")
            or "gemma4:e2b"
        ).strip()

        reply = send_platform_chat_messages(
            "local_model",
            "",
            model,
            [
                {"role": "system", "content": "你正在做本地模型连通性测试。请只做最简短回复。"},
                {"role": "user", "content": "请只回复“本地模型可用”这六个字。"},
            ],
        )
        if reply:
            return {
                "ok": True,
                "message": "本地模型测试成功",
                "model": model,
                "reply": reply.strip(),
                "local_model": get_local_model_manager().get_status(),
            }
        last_error = get_platform_last_error("local_model") or "本地模型测试失败"
        return {
            "ok": False,
            "message": last_error,
            "model": model,
            "reply": "",
            "local_model": get_local_model_manager().get_status(),
        }

    def _get_query_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        config = self.load_config()
        global_mode = str(config.get("detection_mode", "browser") or "browser").strip()
        if global_mode == "recognition":
            return []
        query_tasks: list[dict[str, Any]] = []
        for task in tasks:
            keywords = task.get("keywords", []) or []
            if not keywords and task.get("keyword"):
                keywords = [{"keyword": task.get("keyword", ""), "platforms": [task.get("platform", "")]}]
            if any(
                str((kw or {}).get("keyword") or "").strip()
                and bool((kw or {}).get("platforms"))
                for kw in keywords
            ):
                query_tasks.append(task)
        return query_tasks

    def _ensure_scheduler(self, config: dict | None = None) -> SmartScheduler:
        current_config = config or self.load_config()
        scheduler_cfg = _build_scheduler_runtime_config(current_config)
        if self._scheduler is None:
            self._scheduler = SmartScheduler(scheduler_cfg)
        else:
            self._scheduler.config = scheduler_cfg
            if hasattr(self._scheduler, "_refresh_config"):
                self._scheduler._refresh_config()
        return self._scheduler

    def _is_monitoring_running(self) -> bool:
        return bool(self._scheduler and self._scheduler.get_status().get("running"))

    def _execute_scheduled_task(self, task: dict) -> dict:
        if self._is_cloud_viewer_account():
            return {"ok": False, "message": "浏览账号仅可查看管理员分配的数据，不能运行任务"}
        config = self.load_config()
        default_notification = config.get("default_notification", {}) or {}
        result = run_task_group(
            task,
            default_notification,
            config,
            execution_source="auto",
            return_report=True,
            stop_checker=(self._scheduler.should_stop if self._scheduler else None),
            platform_session_manager=self._query_session_manager,
        )
        if isinstance(result, tuple):
            _, report = result
            return report
        return {}

    def _create_query_session_manager(
        self,
        tasks: list[dict],
        config: dict | None,
        mode: str,
        *,
        source_label: str,
    ) -> PlatformSessionManager | None:
        normalized_mode = str(mode or "").strip()
        if normalized_mode != "browser":
            return None
        policy = build_query_execution_policy(config or {}, normalized_mode)
        if not policy.use_session_pool:
            return None
        manager = PlatformSessionManager(
            normalized_mode,
            policy,
            build_round_query_plan(tasks or [], normalized_mode),
            logger=print,
        )
        print(f"[WebBackend] {source_label} 启用平台会话池策略: mode={normalized_mode}")
        return manager

    def begin_mode_round(self, mode: str, ordered_units: list[dict], current_date: datetime, scheduler_runtime_config: dict) -> None:
        del current_date, scheduler_runtime_config
        config = self.load_config()
        self._reap_inactive_recognition_test_session(restore_previous=True)
        if self._recognition_test_manager is None:
            recognition_manager = self._ensure_recognition_manager()
            recognition_status = {}
            if recognition_manager is not None:
                try:
                    recognition_status = recognition_manager.get_runtime_status() or {}
                except Exception:
                    recognition_status = {}
                if recognition_manager.has_recognition_tasks() and not recognition_status.get("running"):
                    try:
                        recognition_manager.start()
                        print("[WebBackend] 定时轮次开始，已启动识别监听")
                    except Exception as exc:
                        print(f"[WebBackend] 定时轮次启动识别监听失败: {exc}")
        if mode == "browser":
            policy = build_query_execution_policy(config, mode)
            self._query_session_manager = PlatformSessionManager(
                mode,
                policy,
                build_round_query_plan(ordered_units, mode),
                logger=print,
            )
            print(f"[WebBackend] {mode} 模式启用平台会话池策略")
        else:
            self._query_session_manager = None
        self._query_session_mode = str(mode or "").strip()

    def end_mode_round(
        self,
        mode: str,
        ordered_units: list[dict],
        current_date: datetime,
        mode_reports: list[dict],
        cancelled: bool,
    ) -> None:
        del mode, ordered_units, current_date, mode_reports, cancelled
        if self._query_session_manager is not None:
            self._query_session_manager.close_all(reason="当前模式轮次结束")
        self._query_session_manager = None
        self._query_session_mode = ""

    def _on_scheduler_status_change(self, status: str, message: str) -> None:
        self._monitoring_status_message = str(message or "").strip() or (
            "定时任务运行中" if self._is_monitoring_running() else "定时任务已关闭"
        )
        if self._scheduler_reporter is not None:
            try:
                self._scheduler_reporter.handle_status_event(status, message)
            except Exception:
                pass

    def _on_scheduler_task_timeout(self, task: dict, elapsed_seconds: float) -> None:
        task_name = str(task.get("name") or task.get("task_id") or "未命名任务").strip()
        print(f"[WebBackend] 调度任务超时: {task_name}, elapsed={int(elapsed_seconds)}s")
        if self._scheduler_reporter is not None:
            try:
                self._scheduler_reporter.send_timeout(task, elapsed_seconds)
            except Exception:
                pass

    def _on_scheduler_round_complete(self, payload: dict[str, Any]) -> None:
        if self._scheduler_reporter is not None:
            try:
                self._scheduler_reporter.send_mode_summary(payload)
            except Exception:
                pass
        self._process_pending_task_deletions()

    def _on_scheduler_cycle_complete(self, payload: dict[str, Any]) -> None:
        if self._scheduler_reporter is not None:
            try:
                self._scheduler_reporter.send_cycle_summary(payload)
            except Exception:
                pass
        self._process_pending_task_deletions()

    def _on_recognition_round_complete(self, payload: dict[str, Any]) -> None:
        if self._scheduler_reporter is not None:
            try:
                self._scheduler_reporter.send_recognition_round_summary(payload)
            except Exception:
                pass
        self._process_pending_task_deletions()

    def start_monitoring(self) -> dict:
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        config = self.load_config()
        enabled_tasks = [task for task in (config.get("tasks", []) or []) if task.get("enabled", True)]
        query_tasks = self._get_query_tasks(enabled_tasks)
        if not query_tasks:
            current_mode = str(config.get("detection_mode", "browser") or "browser").strip()
            if current_mode == "recognition":
                message = "识别模式不参与自动调度，请切回抓取模式后开启"
                self._monitoring_status_message = message
                return {"ok": False, "enabled": False, "message": message}
            else:
                message = "暂无可运行的定时任务"
            self._monitoring_status_message = message
            set_auto_resume_monitoring(False)
            return {"ok": False, "enabled": False, "message": message}

        scheduler = self._ensure_scheduler(config)
        if scheduler.get_status().get("running"):
            set_auto_resume_monitoring(True)
            self._monitoring_status_message = "定时任务已开启"
            return {"ok": True, "enabled": True, "message": "定时任务已开启"}

        scheduler.start(
            query_tasks,
            self._execute_scheduled_task,
            self._on_scheduler_status_change,
            on_task_timeout=self._on_scheduler_task_timeout,
            on_round_complete=self._on_scheduler_round_complete,
            on_cycle_complete=self._on_scheduler_cycle_complete,
        )
        set_auto_resume_monitoring(True)
        self._monitoring_status_message = "定时任务已开启"
        return {"ok": True, "enabled": True, "message": "已开启定时任务"}

    def stop_monitoring(self, *, persist_preference: bool = True) -> dict:
        if self._scheduler:
            self._scheduler.stop()
        if self._query_session_manager is not None:
            self._query_session_manager.close_all(reason="定时任务已关闭")
            self._query_session_manager = None
        self._query_session_mode = ""
        if persist_preference:
            set_auto_resume_monitoring(False)
        self._monitoring_status_message = "定时任务已关闭"
        return {"ok": True, "enabled": False, "message": "已关闭定时任务"}

    def set_monitoring_enabled(self, payload: dict[str, Any]) -> dict:
        enabled = bool(payload.get("enabled", False))
        if enabled:
            blocked = self._viewer_execution_block_response()
            if blocked:
                return blocked
        return self.start_monitoring() if enabled else self.stop_monitoring()

    def _refresh_monitoring_runtime(self, *, restart_scheduler: bool = True) -> None:
        config = self.load_config()
        if self._scheduler is not None:
            self._ensure_scheduler(config)
        if not should_auto_resume_monitoring():
            return
        if restart_scheduler:
            if self._is_monitoring_running():
                self.stop_monitoring(persist_preference=False)
            self.start_monitoring()
            return
        if self._is_monitoring_running():
            self._monitoring_status_message = "定时任务已开启"
            return
        self.start_monitoring()

    def start_account_crawl_scheduler(self) -> None:
        if self._account_crawl_thread and self._account_crawl_thread.is_alive():
            return
        self._account_crawl_stop_event.clear()

        def _worker() -> None:
            while not self._account_crawl_stop_event.is_set():
                try:
                    config = self.load_config()
                    if should_run_scheduled_crawl(config):
                        self.run_account_article_crawl({"trigger": "auto"})
                except Exception as exc:
                    print(f"[WebBackend] 账号文章定时抓取失败: {exc}")
                self._account_crawl_stop_event.wait(30)

        self._account_crawl_thread = threading.Thread(
            target=_worker,
            daemon=True,
            name="account-article-crawler",
        )
        self._account_crawl_thread.start()

    def stop_account_crawl_scheduler(self) -> None:
        self._account_crawl_stop_event.set()
        thread = self._account_crawl_thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self._account_crawl_thread = None

    def run_account_article_crawl(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        blocked = self._viewer_execution_block_response(
            fetched_count=0,
            added_count=0,
            duplicate_count=0,
            excluded_count=0,
            excluded_links=[],
            results=[],
        )
        if blocked:
            return blocked
        normalized_payload = payload or {}
        if not self._account_crawl_lock.acquire(blocking=False):
            return {
                "ok": False,
                "message": "已有账号抓取任务正在运行，请稍后再试",
                "fetched_count": 0,
                "added_count": 0,
                "duplicate_count": 0,
                "excluded_count": 0,
                "excluded_links": [],
                "results": [],
            }
        try:
            raw_account_ids = normalized_payload.get("account_ids") or []
            account_ids = [
                str(account_id or "").strip()
                for account_id in raw_account_ids
                if str(account_id or "").strip()
            ] if isinstance(raw_account_ids, list) else []
            trigger = str(normalized_payload.get("trigger") or "manual").strip().lower()
            if trigger != "auto":
                trigger = "manual"
            result = crawl_account_articles(
                self.load_config(),
                account_ids=account_ids,
                trigger=trigger,
            )
            if int(result.get("added_count") or 0) > 0 or int(result.get("duplicate_count") or 0) > 0:
                self._invalidate_article_cache()
            print(
                "[WebBackend] 账号文章抓取完成",
                {
                    "trigger": trigger,
                    "fetched": result.get("fetched_count"),
                    "added": result.get("added_count"),
                    "duplicate": result.get("duplicate_count"),
                },
            )
            return result
        finally:
            self._account_crawl_lock.release()

    def restore_account_crawl_exclusions(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        raw_urls = request_payload.get("urls")
        urls = [
            str(url or "").strip()
            for url in raw_urls
            if str(url or "").strip()
        ] if isinstance(raw_urls, list) else []
        result = restore_excluded_article_urls(urls)
        excluded_links = get_excluded_article_urls(limit=100000)
        result["excluded_links"] = excluded_links
        result["total"] = len(excluded_links)
        return result

    def get_account_crawl_exclusions(self) -> dict[str, Any]:
        excluded_links = get_excluded_article_urls(limit=100000)
        return {
            "ok": True,
            "total": len(excluded_links),
            "excluded_links": excluded_links,
        }

    def restore_monitoring_if_needed(self) -> None:
        self._cloud_runtime.start()
        self.start_account_crawl_scheduler()
        if should_auto_resume_monitoring():
            self.start_monitoring()

    def shutdown(self) -> None:
        self._cloud_runtime.stop()
        support = getattr(self, "_cloud_runtime_support", None)
        if isinstance(support, AppCloudRuntimeSupport):
            support.stop()
        self._stop_cloud_command_transport()
        self.stop_account_crawl_scheduler()
        self._stop_recognition_test_session(restore_previous=False)
        if self._is_monitoring_running():
            self.stop_monitoring(persist_preference=False)
        self._close_manual_platform_session_manager(reason="Web 后端正在关闭")
        self._close_recognition_shared_browser()
        for platform_name in self._browser_auth_state.list_platforms():
            self._close_browser_auth_session(platform_name, reason="Web 后端正在关闭")
        mgr = self._recognition_manager
        if mgr and mgr.get_runtime_status().get("running", False):
            try:
                mgr.stop()
            except Exception:
                pass
        try:
            get_local_model_manager().shutdown_owned_process()
        except Exception:
            pass

    def request_app_exit(self, delay_seconds: float = 0.8) -> bool:
        callback = self._exit_callback
        if not callable(callback):
            return False

        def _runner():
            try:
                time.sleep(max(0.0, float(delay_seconds or 0.0)))
                callback()
            except Exception:
                pass

        threading.Thread(target=_runner, daemon=True).start()
        return True

    def pick_directory(self, payload: dict | None = None) -> dict:
        callback = self._directory_picker_callback
        if not callable(callback):
            return {"ok": False, "message": "当前运行环境不支持原生目录选择器，请手动填写路径"}
        normalized_payload = payload or {}
        title = str(normalized_payload.get("title", "") or "选择目录").strip() or "选择目录"
        try:
            selected = str(callback(title) or "").strip()
        except Exception as exc:
            return {"ok": False, "message": f"打开目录选择器失败：{exc}"}
        if not selected:
            return {"ok": False, "message": "你已取消目录选择"}
        selected_path = Path(selected).expanduser().resolve()
        return {
            "ok": True,
            "path": str(selected_path),
            "message": "已选择目录",
        }

    def _normalize_and_store_todos(self, config: dict, *, save: bool = False) -> list[dict[str, Any]]:
        todos, changed = normalize_quick_todos(config.get("quick_todos", []))
        if changed or config.get("quick_todos") != todos:
            config["quick_todos"] = todos
            if save:
                self.save_config(config)
        return todos

    def _get_synced_articles(self, config: dict | None = None) -> list[dict[str, Any]]:
        """返回已按当前任务配置刷新归属关系的文章列表。"""
        resolved_config = config or self.load_config()
        session = CloudSessionStore().load()
        cache_key = (
            self._article_store_version_key(),
            self._article_match_config_key(resolved_config),
            self._article_visibility_session_key(session),
        )
        with self._article_cache_lock:
            cache = self._synced_articles_cache
            if cache and cache.get("key") == cache_key:
                return list(cache.get("articles") or [])

            schedule_result = schedule_article_match_refresh(resolved_config, reason="cloud_sync_read")
            deferred_refresh = _article_match_refresh_is_deferred(schedule_result)
            if deferred_refresh:
                articles = _apply_articles_account_context(
                    get_articles(),
                    resolved_config,
                )
            else:
                articles = _apply_articles_account_context(
                    refresh_article_matches(resolved_config),
                    resolved_config,
                )
            articles = self._filter_articles_for_current_cloud_visibility(
                articles,
                resolved_config,
                session=session,
            )
            if deferred_refresh:
                return list(articles)
            final_key = (
                self._article_store_version_key(),
                cache_key[1],
                cache_key[2],
            )
            self._synced_articles_cache = {
                "key": final_key,
                "articles": articles,
            }
            return list(articles)

    def _get_sqlite_shadow_article_page(
        self,
        config: dict[str, Any],
        *,
        media_type: str = "",
        limit: int = 50,
        task_name: str = "",
    ) -> dict[str, Any] | None:
        read_backend, backend_source, raw_backend = _article_read_backend_with_source()
        if not read_backend:
            return None
        resolved_limit = max(1, int(limit or 50))
        if resolved_limit > ARTICLE_SQLITE_PAGE_LIMIT_GUARD:
            self._record_sqlite_shadow_fallback(read_backend, "limit_exceeded")
            return None
        cooldown_reason = self._sqlite_shadow_cooldown_reason()
        if cooldown_reason:
            self._record_sqlite_shadow_fallback(read_backend, cooldown_reason)
            return None
        started = time.perf_counter()
        db_path = default_shadow_db_path()
        fd_before = _open_sqlite_fd_count(db_path)
        session = CloudSessionStore().load()
        if self._is_ordinary_cloud_session(session):
            self._record_sqlite_shadow_fallback(read_backend, "ordinary_cloud_session")
            return None

        try:
            type_map = {"media": "authority", "self-media": "selfmedia"}
            normalized_media_type = type_map.get(str(media_type or "").strip(), str(media_type or "").strip())
            if not self._ensure_sqlite_shadow_article_index(config, db_path=db_path):
                self._record_sqlite_shadow_fallback(
                    read_backend,
                    str(self._article_sqlite_shadow_unready_reason or "article_shadow_not_fresh"),
                )
                return None

            store = ArticleHistorySQLiteStore(
                db_path,
                normalize_article_url=normalize_article_url,
                sqlite_timeout=0.2,
            )
            page = call_with_locked_retry(
                lambda: store.get_article_page(
                    limit=resolved_limit,
                    offset=0,
                    task_name=str(task_name or "").strip(),
                    media_type=normalized_media_type,
                    today=local_today().isoformat(),
                )
            )
            if bool(page.get("fallback_required")):
                self._record_sqlite_shadow_fallback(
                    read_backend,
                    str(page.get("fallback_reason") or "article_page_fallback_required"),
                )
                return None

            articles = _apply_articles_account_context(
                [
                    item for item in (page.get("items") or [])
                    if isinstance(item, dict)
                ],
                config,
            )
            result = {
                "articles": articles,
                "total": int(page.get("total") or 0),
                "today_total": int(page.get("today_total") or 0),
                "compare_only": read_backend == "sqlite_shadow_compare",
            }
            result["requested_backend"] = read_backend
            result["backend_source"] = backend_source
            result["raw_backend"] = raw_backend
        except Exception as exc:
            self._record_sqlite_shadow_failure(
                read_backend,
                "read_error",
                detail=f"{exc.__class__.__name__}: {exc}",
                elapsed_ms=_elapsed_ms_since(started),
                fd_before=fd_before,
                fd_after=_open_sqlite_fd_count(db_path),
                db_path=db_path,
            )
            print(f"[WebBackend] SQLite 文章页读取失败，回退 JSON: {exc}")
            return None

        fd_after = _open_sqlite_fd_count(db_path)
        elapsed_ms = _elapsed_ms_since(started)
        if self._sqlite_shadow_fd_growth_exceeded(fd_before, fd_after):
            self._record_sqlite_shadow_failure(
                read_backend,
                "fd_growth",
                detail=f"{fd_before}->{fd_after}",
                elapsed_ms=elapsed_ms,
                fd_before=fd_before,
                fd_after=fd_after,
                db_path=db_path,
            )
            return None
        self._record_sqlite_shadow_success(
            read_backend,
            elapsed_ms=elapsed_ms,
            fd_before=fd_before,
            fd_after=fd_after,
            db_path=db_path,
        )
        return result

    def _record_sqlite_shadow_article_compare(
        self,
        *,
        query: dict[str, Any],
        sqlite_page: dict[str, Any],
        json_result: dict[str, Any],
    ) -> None:
        json_articles = [
            item for item in (json_result.get("articles") or [])
            if isinstance(item, dict)
        ]
        sqlite_articles = [
            item for item in (sqlite_page.get("articles") or [])
            if isinstance(item, dict)
        ]
        json_ids = [str(item.get("id") or "").strip() for item in json_articles]
        sqlite_ids = [str(item.get("id") or "").strip() for item in sqlite_articles]
        json_total = int(json_result.get("total") or 0)
        sqlite_total = int(sqlite_page.get("total") or 0)
        json_today_total = int(json_result.get("today_total") or 0)
        sqlite_today_total = int(sqlite_page.get("today_total") or 0)

        mismatches: list[str] = []
        if json_total != sqlite_total:
            mismatches.append("total")
        if json_today_total != sqlite_today_total:
            mismatches.append("today_total")
        if json_ids != sqlite_ids:
            mismatches.append("article_ids")

        entry = {
            "ok": not mismatches,
            "checked_at": local_now().isoformat(timespec="seconds"),
            "query": dict(query or {}),
            "mismatches": mismatches,
            "json": {
                "total": json_total,
                "today_total": json_today_total,
                "ids": json_ids,
            },
            "sqlite": {
                "total": sqlite_total,
                "today_total": sqlite_today_total,
                "ids": sqlite_ids,
            },
        }
        with self._article_sqlite_shadow_compare_lock:
            self._article_sqlite_shadow_compare_recent.append(entry)
            self._article_sqlite_shadow_compare_recent = self._article_sqlite_shadow_compare_recent[-50:]
        if mismatches:
            self._record_sqlite_shadow_failure(
                _article_read_backend_with_source()[0],
                "compare_mismatch",
                detail=",".join(mismatches),
            )
            print("[WebBackend] SQLite 文章页影子比对不一致", entry)

    def get_article_sqlite_shadow_compare_status(self) -> dict[str, Any]:
        with self._article_sqlite_shadow_compare_lock:
            recent = list(self._article_sqlite_shadow_compare_recent)
        mismatch_count = sum(1 for item in recent if not bool(item.get("ok")))
        requested_backend, backend_source, raw_backend = _article_read_backend_with_source()
        db_path = default_shadow_db_path()
        readiness = ArticleHistorySQLiteStore.validate_readiness(db_path)
        article_meta = _read_sqlite_store_meta(
            db_path,
            [
                "article_source_signature",
                "article_match_config_signature",
                "article_last_rebuilt_at",
                "article_last_incremental_sync_at",
                "article_last_dirty_reason",
            ],
        ) if bool(readiness.get("ready")) else {}
        expected_source_signature = ""
        expected_match_signature = ""
        try:
            expected_source_signature = str(get_article_source_signature() or "").strip()
        except Exception:
            expected_source_signature = ""
        try:
            config = self.load_config()
            expected_match_signature = str(self._article_match_config_key(config) or "").strip()
        except Exception:
            expected_match_signature = ""
        stored_source_signature = str(article_meta.get("article_source_signature") or "").strip()
        stored_match_signature = str(article_meta.get("article_match_config_signature") or "").strip()
        source_fresh = (
            stored_source_signature == expected_source_signature
            if expected_source_signature
            else None
        )
        match_fresh = (
            stored_match_signature == expected_match_signature
            if expected_match_signature
            else None
        )
        cooldown_reason = self._sqlite_shadow_cooldown_reason()
        try:
            session = CloudSessionStore().load()
            ordinary_cloud_session = self._is_ordinary_cloud_session(session)
        except Exception:
            ordinary_cloud_session = False
        fallback_reason = ""
        effective_backend = "json"
        if not requested_backend:
            fallback_reason = "disabled"
        elif requested_backend == "sqlite_shadow_compare":
            fallback_reason = "compare_only"
        elif ordinary_cloud_session:
            fallback_reason = "ordinary_cloud_session"
        elif cooldown_reason:
            fallback_reason = cooldown_reason
        elif not bool(readiness.get("ready")):
            fallback_reason = (
                "article_shadow_db_missing"
                if not readiness.get("available")
                else f"article_shadow_db_{readiness.get('reason') or 'not_ready'}"
            )
        elif source_fresh is False:
            fallback_reason = "article_shadow_source_stale"
        elif match_fresh is False:
            fallback_reason = "article_shadow_match_config_stale"
        elif source_fresh is True and match_fresh is True:
            effective_backend = "sqlite_shadow"
        else:
            fallback_reason = "article_shadow_freshness_unknown"
        rebuild_lock = getattr(self, "_article_sqlite_shadow_rebuild_lock", None)
        if rebuild_lock is None:
            rebuild_state = dict(getattr(self, "_article_sqlite_shadow_rebuild_state", {}) or {})
        else:
            with rebuild_lock:
                rebuild_state = dict(getattr(self, "_article_sqlite_shadow_rebuild_state", {}) or {})
        return {
            "ok": True,
            "mode": requested_backend,
            "requested_backend": requested_backend,
            "backend_source": backend_source,
            "raw_backend": raw_backend,
            "effectiveBackend": effective_backend,
            "effective_backend": effective_backend,
            "fallbackReason": fallback_reason,
            "fallback_reason": fallback_reason,
            "checked": len(recent),
            "mismatches": mismatch_count,
            "health": self._article_sqlite_shadow_health_snapshot(),
            "readiness": readiness,
            "freshness": {
                "fresh": source_fresh is True and match_fresh is True,
                "source_fresh": source_fresh,
                "match_config_fresh": match_fresh,
                "stored_article_source_signature": stored_source_signature,
                "current_article_source_signature": expected_source_signature,
                "stored_article_match_config_signature": stored_match_signature,
                "current_article_match_config_signature": expected_match_signature,
                "last_rebuilt_at": str(article_meta.get("article_last_rebuilt_at") or ""),
                "last_incremental_sync_at": str(article_meta.get("article_last_incremental_sync_at") or ""),
                "last_dirty_reason": str(article_meta.get("article_last_dirty_reason") or ""),
            },
            "rebuild": rebuild_state,
            "last": recent[-1] if recent else None,
            "recent": recent,
        }

    def _sqlite_shadow_cooldown_reason(self) -> str:
        now_ts = time.monotonic()
        with self._article_sqlite_shadow_health_lock:
            disabled_until = _safe_float(
                self._article_sqlite_shadow_health.get("disabled_until_monotonic"),
                _safe_float(self._article_sqlite_shadow_health.get("disabled_until"), 0.0),
            )
            if disabled_until > now_ts:
                return str(self._article_sqlite_shadow_health.get("last_fallback_reason") or "health_cooldown")
        return ""

    def _record_sqlite_shadow_success(
        self,
        mode: str,
        *,
        elapsed_ms: float,
        fd_before: int | None,
        fd_after: int | None,
        db_path: Path,
    ) -> None:
        with self._article_sqlite_shadow_health_lock:
            self._article_sqlite_shadow_health.update({
                "consecutive_errors": 0,
                "last_mode": str(mode or ""),
                "last_effective_backend": "json" if str(mode or "") == "sqlite_shadow_compare" else "sqlite_shadow",
                "last_probe_backend": "sqlite_shadow",
                "last_success_at": local_now().isoformat(timespec="seconds"),
                "last_elapsed_ms": round(float(elapsed_ms), 3),
                "disabled_until": 0.0,
                "disabled_until_monotonic": 0.0,
                "disabled_until_iso": "",
                "last_fd_before": fd_before,
                "last_fd_after": fd_after,
                "last_fd_delta": _fd_delta(fd_before, fd_after),
                "last_db_path": str(db_path),
            })

    def _record_sqlite_shadow_fallback(self, mode: str, reason: str) -> None:
        with self._article_sqlite_shadow_health_lock:
            self._article_sqlite_shadow_health.update({
                "last_mode": str(mode or ""),
                "last_effective_backend": "json",
                "last_probe_backend": "",
                "last_fallback_reason": str(reason or ""),
                "last_fallback_at": local_now().isoformat(timespec="seconds"),
                "fallback_count": _safe_int(self._article_sqlite_shadow_health.get("fallback_count"), 0) + 1,
            })

    def _record_sqlite_shadow_failure(
        self,
        mode: str,
        reason: str,
        *,
        detail: str = "",
        elapsed_ms: float | None = None,
        fd_before: int | None = None,
        fd_after: int | None = None,
        db_path: Path | None = None,
    ) -> None:
        now_ts = time.monotonic()
        now_wall = local_now()
        error_limit = self._sqlite_shadow_error_limit()
        cooldown_seconds = self._sqlite_shadow_cooldown_seconds()
        with self._article_sqlite_shadow_health_lock:
            consecutive = _safe_int(self._article_sqlite_shadow_health.get("consecutive_errors"), 0) + 1
            force_cooldown = str(reason or "") in {"compare_mismatch", "fd_growth", "index_verify_failed"}
            disabled_until = now_ts + cooldown_seconds if force_cooldown or consecutive >= error_limit else 0.0
            self._article_sqlite_shadow_health.update({
                "consecutive_errors": consecutive,
                "last_mode": str(mode or ""),
                "last_effective_backend": "json",
                "last_probe_backend": "",
                "last_fallback_reason": str(reason or ""),
                "last_fallback_at": local_now().isoformat(timespec="seconds"),
                "last_error": str(detail or reason or ""),
                "last_error_at": local_now().isoformat(timespec="seconds"),
                "fallback_count": _safe_int(self._article_sqlite_shadow_health.get("fallback_count"), 0) + 1,
                "disabled_until": disabled_until,
                "disabled_until_monotonic": disabled_until,
                "disabled_until_iso": (
                    (now_wall + timedelta(seconds=cooldown_seconds)).isoformat(timespec="seconds")
                    if disabled_until > 0
                    else ""
                ),
                "tripped_count": (
                    _safe_int(self._article_sqlite_shadow_health.get("tripped_count"), 0) + 1
                    if disabled_until > 0
                    else _safe_int(self._article_sqlite_shadow_health.get("tripped_count"), 0)
                ),
            })
            if elapsed_ms is not None:
                self._article_sqlite_shadow_health["last_elapsed_ms"] = round(float(elapsed_ms), 3)
            if fd_before is not None or fd_after is not None:
                self._article_sqlite_shadow_health["last_fd_before"] = fd_before
                self._article_sqlite_shadow_health["last_fd_after"] = fd_after
                self._article_sqlite_shadow_health["last_fd_delta"] = _fd_delta(fd_before, fd_after)
            if db_path is not None:
                self._article_sqlite_shadow_health["last_db_path"] = str(db_path)

    def _article_sqlite_shadow_health_snapshot(self) -> dict[str, Any]:
        now_ts = time.monotonic()
        with self._article_sqlite_shadow_health_lock:
            health = dict(self._article_sqlite_shadow_health)
        disabled_until = _safe_float(
            health.get("disabled_until_monotonic"),
            _safe_float(health.get("disabled_until"), 0.0),
        )
        health["blocked"] = disabled_until > now_ts
        health["cooldown_remaining_seconds"] = max(0, round(disabled_until - now_ts, 3)) if disabled_until > now_ts else 0
        health["error_limit"] = self._sqlite_shadow_error_limit()
        health["cooldown_seconds"] = self._sqlite_shadow_cooldown_seconds()
        health["fd_growth_limit"] = self._sqlite_shadow_fd_growth_limit()
        return health

    @staticmethod
    def _sqlite_shadow_error_limit() -> int:
        return max(1, _safe_int(os.environ.get("AIBRANDMONITOR_ARTICLE_SQLITE_ERROR_LIMIT"), 3))

    @staticmethod
    def _sqlite_shadow_cooldown_seconds() -> int:
        return max(1, _safe_int(os.environ.get("AIBRANDMONITOR_ARTICLE_SQLITE_COOLDOWN_SECONDS"), 60))

    @staticmethod
    def _sqlite_shadow_fd_growth_limit() -> int:
        return _safe_int(os.environ.get("AIBRANDMONITOR_ARTICLE_SQLITE_FD_GROWTH_LIMIT"), 8)

    def _sqlite_shadow_fd_growth_exceeded(self, fd_before: int | None, fd_after: int | None) -> bool:
        limit = self._sqlite_shadow_fd_growth_limit()
        if limit < 0 or fd_before is None or fd_after is None:
            return False
        return int(fd_after) - int(fd_before) > limit

    @staticmethod
    def _sqlite_shadow_article_rebuild_min_interval_seconds() -> float:
        return max(0.0, _safe_float(os.environ.get("AIBRANDMONITOR_ARTICLE_SQLITE_REBUILD_MIN_INTERVAL_SECONDS"), 30.0))

    def _ensure_sqlite_shadow_article_index(self, config: dict[str, Any], *, db_path: Path) -> bool:
        match_key = self._article_match_config_key(config)
        source_signature = get_article_source_signature()
        current_key = (str(db_path), source_signature, match_key)
        with self._article_sqlite_shadow_lock:
            if self._article_sqlite_shadow_cache_key == current_key and db_path.exists():
                self._article_sqlite_shadow_unready_reason = ""
                return True

            readiness = ArticleHistorySQLiteStore.validate_readiness(db_path)
            if not bool(readiness.get("ready")):
                reason = "article_shadow_db_missing" if not readiness.get("available") else f"article_shadow_db_{readiness.get('reason') or 'not_ready'}"
                self._article_sqlite_shadow_cache_key = None
                self._article_sqlite_shadow_unready_reason = reason
                self._schedule_sqlite_shadow_article_rebuild(config, db_path=db_path, reason=reason)
                return False

            store = ArticleHistorySQLiteStore(
                db_path,
                normalize_article_url=normalize_article_url,
                sqlite_timeout=0.2,
            )
            stored_source_signature = str(store.get_meta("article_source_signature") or "").strip()
            stored_match_signature = str(store.get_meta("article_match_config_signature") or "").strip()
            if stored_source_signature == source_signature and stored_match_signature == match_key:
                self._article_sqlite_shadow_cache_key = current_key
                self._article_sqlite_shadow_unready_reason = ""
                return True

            if stored_source_signature != source_signature:
                reason = "article_shadow_source_stale"
            else:
                reason = "article_shadow_match_config_stale"
            self._article_sqlite_shadow_cache_key = None
            self._article_sqlite_shadow_unready_reason = reason
            self._schedule_sqlite_shadow_article_rebuild(config, db_path=db_path, reason=reason)
            return False

    def _schedule_sqlite_shadow_article_rebuild(
        self,
        config: dict[str, Any],
        *,
        db_path: Path,
        reason: str,
    ) -> bool:
        now_ts = time.monotonic()
        with self._article_sqlite_shadow_rebuild_lock:
            if bool(self._article_sqlite_shadow_rebuild_state.get("running")):
                return False
            next_allowed_at = _safe_float(
                self._article_sqlite_shadow_rebuild_state.get("next_allowed_at_monotonic"),
                _safe_float(self._article_sqlite_shadow_rebuild_state.get("next_allowed_at"), 0.0),
            )
            if next_allowed_at > now_ts:
                return False
            next_allowed = now_ts + self._sqlite_shadow_article_rebuild_min_interval_seconds()
            self._article_sqlite_shadow_rebuild_state.update({
                "running": True,
                "last_reason": str(reason or ""),
                "last_started_at": now_ts,
                "last_started_at_monotonic": now_ts,
                "last_started_at_iso": local_now().isoformat(timespec="seconds"),
                "next_allowed_at": next_allowed,
                "next_allowed_at_monotonic": next_allowed,
                "db_path": str(db_path),
            })

        thread = threading.Thread(
            target=self._run_sqlite_shadow_article_rebuild,
            args=(dict(config or {}), Path(db_path), str(reason or "")),
            name="article-sqlite-shadow-rebuild",
            daemon=True,
        )
        thread.start()
        return True

    def _run_sqlite_shadow_article_rebuild(self, config: dict[str, Any], db_path: Path, reason: str) -> None:
        ok = False
        detail = ""
        try:
            match_key = self._article_match_config_key(config)
            articles = refresh_article_matches(config)
            imported_source_signature = get_article_source_signature()
            store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
            store.set_meta("article_source_signature", "")
            import_result = store.import_articles(articles, replace=True)
            if not self._verify_sqlite_shadow_article_index(store, articles):
                print(
                    "[WebBackend] SQLite 文章影子索引校验失败，回退 JSON",
                    {
                        "db_path": str(db_path),
                        "import_result": import_result,
                    },
                )
                self._article_sqlite_shadow_cache_key = None
                detail = "verification_failed"
                return

            final_source_signature = get_article_source_signature()
            if imported_source_signature != final_source_signature:
                self._article_sqlite_shadow_cache_key = None
                detail = "source_changed_during_rebuild"
                return

            store.set_meta("article_source_signature", final_source_signature)
            store.set_meta("article_match_config_signature", match_key)
            store.set_meta("article_last_rebuilt_at", local_now().isoformat(timespec="seconds"))
            self._article_sqlite_shadow_cache_key = (str(db_path), final_source_signature, match_key)
            ok = True
        except Exception as exc:
            detail = f"{exc.__class__.__name__}: {exc}"
            self._article_sqlite_shadow_cache_key = None
            print(f"[WebBackend] SQLite 文章影子索引后台重建失败: {exc}")
        finally:
            with self._article_sqlite_shadow_rebuild_lock:
                finished_at = time.monotonic()
                self._article_sqlite_shadow_rebuild_state.update({
                    "running": False,
                    "last_ok": ok,
                    "last_finished_at": finished_at,
                    "last_finished_at_monotonic": finished_at,
                    "last_finished_at_iso": local_now().isoformat(timespec="seconds"),
                    "last_reason": str(reason or self._article_sqlite_shadow_rebuild_state.get("last_reason") or ""),
                    "last_error": "" if ok else detail,
                })

    @staticmethod
    def _verify_sqlite_shadow_article_index(
        store: ArticleHistorySQLiteStore,
        articles: list[dict[str, Any]],
    ) -> bool:
        raw_articles = [
            item for item in articles
            if isinstance(item, dict)
        ]
        expected_articles = _dedupe_articles_by_url(raw_articles)
        actual_articles = _dedupe_articles_by_url(store.get_article_items())
        if len(actual_articles) != len(expected_articles):
            return False
        expected_task_counts: dict[str, int] = {}
        for article in raw_articles:
            seen: set[str] = set()
            for raw_name in article.get("matched_tasks") or []:
                task_name = str(raw_name or "").strip()
                if not task_name or task_name in seen:
                    continue
                seen.add(task_name)
                expected_task_counts[task_name] = expected_task_counts.get(task_name, 0) + 1
        return store.get_article_task_counts(relation="matched") == dict(sorted(expected_task_counts.items()))

    def _ensure_recognition_manager(self):
        if self._is_cloud_viewer_account():
            return None
        if self._recognition_manager is not None:
            return self._recognition_manager
        try:
            from core.recognition import ClipboardRecognitionManager
            self._recognition_manager = ClipboardRecognitionManager(
                config_getter=self.load_config,
                on_send_complete=(
                    self._scheduler_reporter.record_recognition_result
                    if self._scheduler_reporter is not None
                    else None
                ),
                on_round_complete=(
                    self._on_recognition_round_complete
                ),
            )
        except Exception:
            self._recognition_manager = None
        return self._recognition_manager

    def _prime_manual_test_status(self, task: dict[str, Any], *, message: str = "识别模式测试已启动") -> None:
        task_payload = dict(task or {})
        if not task_payload:
            return
        try:
            write_task_status(
                task_payload,
                status="running",
                source="manual_test",
                scope="test",
                message=message,
                extra=build_task_state_extra(
                    brands=[str(task_payload.get("brand") or task_payload.get("name") or "").strip()],
                    image_count=0,
                    fixed_screenshot_target=max(1, int(task_payload.get("recognition_batch_size", 1) or 1)),
                    completed_by_quota=False,
                ),
            )
        except Exception as exc:
            print(f"[WebBackend] 预写识别测试状态失败，已忽略: {exc}")

    def _schedule_recognition_test_reap(self, *, restore_previous: bool = True, delay_seconds: float = 0.35) -> None:
        def _runner() -> None:
            if delay_seconds > 0:
                threading.Event().wait(delay_seconds)
            self._reap_inactive_recognition_test_session(restore_previous=restore_previous)

        threading.Thread(target=_runner, daemon=True, name="RecognitionTestReaper").start()

    def _reap_inactive_recognition_test_session(self, *, restore_previous: bool = True) -> None:
        manager = self._recognition_test_manager
        if manager is None:
            return
        try:
            running = bool((manager.get_runtime_status() or {}).get("running", False))
        except Exception:
            running = False
        if running:
            return
        session = self._recognition_test_session if isinstance(self._recognition_test_session, dict) else {}
        if bool(session.get("keep_until_closed")):
            return
        self._stop_recognition_test_session(restore_previous=restore_previous)

    def _on_recognition_test_send_complete(self, batch: dict, ok: bool, reason: str) -> None:
        task_name = str((batch or {}).get("task_name") or "").strip()
        outcome = "成功" if ok else f"失败: {reason or '未知错误'}"
        print(f"[WebBackend] 识别测试发送完成: task={task_name or 'unknown'}, result={outcome}")
        self._schedule_recognition_test_reap(restore_previous=True)

    def _on_recognition_test_mode_change(self, payload: dict | None) -> None:
        mode = str((payload or {}).get("mode") or "").strip()
        if mode == "capture":
            self._schedule_recognition_test_reap(restore_previous=True)

    def _get_active_recognition_manager(self, *, init_if_missing: bool = True):
        if self._is_cloud_viewer_account():
            self._reap_inactive_recognition_test_session(restore_previous=False)
            manager = self._recognition_manager
            if manager is not None:
                try:
                    manager.stop()
                except Exception:
                    pass
            return None, "viewer"
        self._reap_inactive_recognition_test_session(restore_previous=True)
        if self._recognition_test_manager is not None:
            return self._recognition_test_manager, "test"
        if init_if_missing:
            return self._ensure_recognition_manager(), "default"
        return self._recognition_manager, "default"

    @staticmethod
    def _task_supports_recognition_warmup(task: dict[str, Any]) -> bool:
        if not bool(task.get("enabled", True)):
            return False

        keywords = task.get("keywords", [])
        if not keywords and task.get("keyword"):
            keywords = [{
                "keyword": task.get("keyword", ""),
                "platforms": [task.get("platform", "")],
            }]

        for kw in keywords:
            if not isinstance(kw, dict):
                continue
            keyword = str(kw.get("keyword") or "").strip()
            if not keyword:
                continue
            platforms = [str(item or "").strip() for item in (kw.get("platforms") or []) if str(item or "").strip()]
            fallback_platform = str(task.get("platform") or "").strip()
            if platforms or fallback_platform:
                return True
        return False

    def _should_background_warm_recognition(self, config: dict[str, Any] | None = None) -> bool:
        current_config = config or self.load_config()
        mode = str(current_config.get("detection_mode", "browser") or "browser").strip()
        tasks = list(current_config.get("tasks", []) or [])
        if mode == "recognition":
            return any(self._task_supports_recognition_warmup(task) for task in tasks)
        return any(
            bool(task.get("recognition_enabled", False)) and self._task_supports_recognition_warmup(task)
            for task in tasks
        )

    def schedule_recognition_warmup(self, *, delay_seconds: float = 1.2) -> None:
        if self._is_cloud_viewer_account():
            return
        if self._recognition_test_manager is not None or self._recognition_manager is not None:
            return

        with self._recognition_warmup_lock:
            if self._recognition_warmup_thread and self._recognition_warmup_thread.is_alive():
                return

            def _runner() -> None:
                if delay_seconds > 0:
                    threading.Event().wait(delay_seconds)
                if self._recognition_test_manager is not None or self._recognition_manager is not None:
                    return
                try:
                    config = self.load_config()
                    if not self._should_background_warm_recognition(config):
                        return
                    manager = self._ensure_recognition_manager()
                    if manager is None:
                        return
                except Exception:
                    return

            thread = threading.Thread(target=_runner, daemon=True, name="RecognitionWarmup")
            self._recognition_warmup_thread = thread
            thread.start()

    @staticmethod
    def _prepare_manual_test_task(task: dict[str, Any], *, recognition_batch_size: int | None = None) -> dict[str, Any]:
        prepared = copy.deepcopy(task or {})
        prepared["enabled"] = True
        prepared["_daily_state_source"] = "manual_test"
        prepared["recognition_enabled"] = True
        if recognition_batch_size is not None:
            prepared["recognition_batch_size"] = max(1, int(recognition_batch_size or 1))
        assign_task_id(prepared)
        return prepared

    def _build_manual_test_runtime_config(
        self,
        task: dict[str, Any],
        *,
        recognition_batch_size: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime_config = copy.deepcopy(self.load_config())
        runtime_config["detection_mode"] = "recognition"
        test_task = self._prepare_manual_test_task(
            task,
            recognition_batch_size=recognition_batch_size,
        )
        runtime_config["tasks"] = [test_task]
        return runtime_config, test_task

    def _stop_recognition_test_session(self, *, restore_previous: bool = True) -> None:
        session = dict(self._recognition_test_session or {})
        manager = self._recognition_test_manager
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                pass
        self._recognition_test_manager = None
        self._recognition_test_runtime_config = None
        self._recognition_test_session = None

        if not restore_previous or not bool(session.get("restore_running")):
            return

        previous_state = session.get("previous_state")
        manager = self._ensure_recognition_manager()
        if manager is None or not manager.has_recognition_tasks():
            return
        try:
            manager.start()
            if previous_state:
                manager.import_runtime_state(previous_state)
        except Exception:
            pass

    def _start_recognition_test_session(self, task: dict[str, Any]) -> dict:
        self._stop_recognition_test_session(restore_previous=True)

        runtime_config, test_task = self._build_manual_test_runtime_config(
            task,
            recognition_batch_size=max(1, int(task.get("recognition_batch_size", 1) or 1)),
        )
        reset_manual_test_session_state(test_task)
        self._prime_manual_test_status(test_task)
        print(
            "[WebBackend] 识别测试任务已构建: "
            f"task={str(test_task.get('name') or '').strip()}, "
            f"batch={int(test_task.get('recognition_batch_size', 1) or 1)}, "
            f"keywords={len(test_task.get('keywords') or [])}"
        )

        previous_state = None
        restore_running = False
        base_manager = self._recognition_manager
        if base_manager is not None and bool(base_manager.get_runtime_status().get("running", False)):
            restore_running = True
            try:
                previous_state = base_manager.export_runtime_state()
            except Exception:
                previous_state = None
            try:
                base_manager.stop()
            except Exception:
                pass

        try:
            from core.recognition import ClipboardRecognitionManager

            self._recognition_test_runtime_config = runtime_config
            self._recognition_test_manager = ClipboardRecognitionManager(
                config_getter=lambda: self._recognition_test_runtime_config or {},
                on_send_complete=self._on_recognition_test_send_complete,
                on_mode_change=self._on_recognition_test_mode_change,
            )
            self._recognition_test_manager.start()
            test_enabled_tasks = self._recognition_test_manager._get_enabled_tasks()
            if test_enabled_tasks:
                debug_preview = ", ".join(
                    f"{item.get('name', '')}(batch={int(item.get('recognition_batch_size', 1) or 1)})"
                    for item in test_enabled_tasks[:5]
                )
                print(f"[WebBackend] 识别测试实际监听任务: {debug_preview}")
        except Exception as exc:
            self._recognition_test_manager = None
            self._recognition_test_runtime_config = None
            self._recognition_test_session = None
            if restore_running and base_manager is not None:
                try:
                    base_manager.start()
                    if previous_state:
                        base_manager.import_runtime_state(previous_state)
                except Exception:
                    pass
            return {"ok": False, "message": f"识别模式测试启动失败：{exc}"}

        self._recognition_test_session = {
            "task_id": str(test_task.get("task_id") or derive_task_id(test_task)).strip(),
            "task_name": str(test_task.get("name") or "").strip(),
            "restore_running": restore_running,
            "previous_state": previous_state,
            "keep_until_closed": True,
        }
        recognition_cfg = dict((runtime_config or {}).get("recognition") or {})
        dom_render_mode = bool(recognition_cfg.get("dom_render_mode", False))
        copy_hint = "请复制新的品牌文本到剪切板。" if dom_render_mode else "请复制新的品牌截图到剪切板。"
        return {
            "ok": True,
            "recognitionTest": True,
            "taskId": self._recognition_test_session["task_id"],
            "taskName": self._recognition_test_session["task_name"],
            "message": f"识别模式测试已启动，{copy_hint}",
        }

    def _sync_recognition_mode(self, config: dict | None = None) -> None:
        if self._is_cloud_viewer_account():
            self._reap_inactive_recognition_test_session(restore_previous=False)
            manager = self._recognition_manager
            if manager is not None:
                try:
                    manager.stop()
                except Exception:
                    pass
            return
        self._reap_inactive_recognition_test_session(restore_previous=True)
        if self._recognition_test_manager is not None:
            return
        config = config or self.load_config()
        mode = str(config.get("detection_mode", "browser") or "browser").strip()
        manager = self._recognition_manager
        if mode != "recognition":
            if manager is not None and bool(manager.get_runtime_status().get("running", False)):
                manager.stop()
            return
        if manager is None:
            self.schedule_recognition_warmup(delay_seconds=0.0)
            return

        running = bool(manager.get_runtime_status().get("running", False))
        if not bool(manager.has_recognition_tasks()) and running:
            manager.stop()

    def snapshot(self) -> dict:
        self._process_pending_task_deletions()
        with self._lock:
            return self._snapshot_locked()

    def session_snapshot(self) -> dict:
        return {
            "ok": True,
            "session": _build_snapshot_session(self.session_token, SESSION_TOKEN_HEADER),
        }

    def get_history_storage_status(self) -> dict[str, Any]:
        db_path = default_shadow_db_path()
        return {
            "ok": True,
            "history": get_structured_read_health(),
            "articles": self.get_article_sqlite_shadow_compare_status(),
            "articleStore": get_article_store_backend_health(),
            "shadowDb": _sqlite_shadow_db_summary(db_path),
        }

    def rebuild_history_sqlite_shadow(self, payload: dict | None = None) -> dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        max_workers_raw = payload.get("workers", payload.get("max_workers"))
        max_workers = _safe_int(max_workers_raw, 0)
        max_workers_arg = max(1, min(16, max_workers)) if max_workers > 0 else None
        verify_tail_limit = max(1, min(100, _safe_int(payload.get("tail_limit"), 20)))
        started = time.perf_counter()
        db_path = default_shadow_db_path()
        with self._history_sqlite_shadow_rebuild_lock:
            result = rebuild_shadow_store(
                db_path,
                max_workers=max_workers_arg,
                verify_tail_limit=verify_tail_limit,
            )
            reset_structured_read_health()
        verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
        return {
            "ok": bool(verification.get("ok")),
            "duration_ms": _elapsed_ms_since(started),
            "db_path": str(db_path),
            "workers": max_workers_arg,
            "tail_limit": verify_tail_limit,
            "configChanged": False,
            "rebuild": result,
            "status": self.get_history_storage_status(),
        }

    def _snapshot_locked(self) -> dict:
        config, _ = self._ensure_context_snapshots(refresh_stale=False)
        self._sync_recognition_mode(config)
        tasks = list(config.get("tasks", []) or [])
        enabled_tasks = [task for task in tasks if task.get("enabled", True)]
        dashboard_today_summary = _build_dashboard_today_task_summary(
            tasks,
            config.get("scheduler", {}),
        )
        active_mode = str(config.get("detection_mode", "browser") or "browser").strip()
        today = local_today().isoformat()

        today_records: list[dict] = []
        platform_counter: Counter[str] = Counter()
        hit_records = 0
        error_records = 0
        history_specs: list[tuple[str, str]] = []
        for task in enabled_tasks:
            task_id = str(task.get("task_id") or derive_task_id(task)).strip()
            task_name = str(task.get("name") or derive_task_id(task)).strip()
            history_specs.append((task_name, task_id))
        try:
            history_batches = get_records_many(history_specs)
        except Exception:
            history_batches = [[] for _ in history_specs]

        for records in history_batches:
            for record in records:
                if is_manual_test_failure_record(record):
                    continue
                if str(record.get("ts", "")).startswith(today):
                    today_records.append(record)
                if is_success_record(record):
                    hit_records += 1
                elif record.get("error_message"):
                    error_records += 1
                platform = str(record.get("platform") or "").strip()
                if platform:
                    platform_counter[platform] += 1

        dashboard_trend = _build_dashboard_trend(enabled_tasks, "week")

        source_breakdown = _build_snapshot_source_breakdown(tasks)

        quick_todos = self._normalize_and_store_todos(config, save=True)
        synced_articles = _apply_articles_account_context(self._get_synced_articles(config), config)
        articles = [_article_to_api(article) for article in synced_articles[:20]]

        recent_events = get_events(limit=5, include_resolved=False)
        pending_reviews = get_pending_reviews(limit=6)
        failed_task_details = _build_dashboard_failed_tasks(
            list(dashboard_today_summary.get("tasks") or [])
        )
        current_date = local_now()

        monitoring_enabled = should_auto_resume_monitoring()
        monitoring_running = self._is_monitoring_running()
        monitoring_status = self._monitoring_status_message or (
            "定时任务已开启" if monitoring_enabled else "定时任务已关闭"
        )
        recognition_status = {}
        mgr, recognition_scope = self._get_active_recognition_manager(init_if_missing=False)
        if mgr and hasattr(mgr, "get_runtime_status"):
            try:
                recognition_status = mgr.get_runtime_status() or {}
            except Exception:
                recognition_status = {}
        if recognition_scope == "test":
            recognition_status["scope"] = "test"
            recognition_status["test_task_id"] = str((self._recognition_test_session or {}).get("task_id") or "").strip()
            recognition_status["test_task_name"] = str((self._recognition_test_session or {}).get("task_name") or "").strip()
        home_copy = _build_dashboard_home_copy(
            config=config,
            mode_key=str(config.get("detection_mode", "browser") or "browser").strip(),
            monitoring_running=monitoring_running,
            recognition_status=recognition_status,
            enabled_task_count=len(enabled_tasks),
            total_task_count=len(tasks),
            failed_task_count=len(failed_task_details),
            today_record_count=len(today_records),
            hit_record_count=hit_records,
        )

        profile_payload = self.get_public_profile(config)
        profile_summary = {
            "name": str(profile_payload.get("name") or "").strip(),
            "role": str(profile_payload.get("role") or "").strip(),
            "avatar": str(profile_payload.get("avatar") or "").strip(),
            "avatarUrl": str(profile_payload.get("avatar") or "").strip(),
            "birthday": str(profile_payload.get("birthday") or "").strip(),
            "hireDate": str(profile_payload.get("hireDate") or profile_payload.get("hire_date") or "").strip(),
        }
        profile_name = profile_summary["name"]

        platform_items = _build_snapshot_platform_items(config.get("platforms", {}) or {})
        tasks_payload = _build_snapshot_task_items(
            enabled_tasks,
            active_mode=active_mode,
            scheduler_config=config.get("scheduler", {}),
            platform_display_name=_pid_to_display,
        )
        tag_options = _collect_snapshot_tag_options(tasks)

        # 可用模型列表
        # 本地模型额外并入运行时已发现的 Ollama 模型，避免模型已安装但“搜搜”下拉不显示。
        available_models = _build_available_models(
            config,
            get_local_model_manager().get_status(),
        )

        version_info = get_version_payload()

        return {
            "generatedAt": current_date.isoformat(timespec="seconds"),
            "session": _build_snapshot_session(self.session_token, SESSION_TOKEN_HEADER),
            "version": version_info,
            "branding": _build_snapshot_branding(APP_NAME),
            "sidebar": _build_snapshot_sidebar(profile_summary),
            "assistant": _build_snapshot_assistant(
                config,
                monitoring_enabled=monitoring_enabled,
                monitoring_running=monitoring_running,
            ),
            "dashboard": _build_snapshot_dashboard(
                current_date=current_date,
                weekday_label=_weekday_label(current_date),
                home_copy=home_copy,
                profile_name=profile_name,
                dashboard_today_summary=dashboard_today_summary,
                failed_task_details=failed_task_details,
                today_record_count=len(today_records),
                hit_record_count=hit_records,
                dashboard_trend=dashboard_trend,
                source_breakdown=source_breakdown,
                task_cards=_build_task_cards(config.get("detection_mode", "browser")),
                media_stats=_build_dashboard_media_stats_monthly(synced_articles),
                month_overview=_build_dashboard_month_overview(
                    enabled_tasks,
                    config.get("scheduler", {}),
                    synced_articles,
                ),
            ),
            "platforms": platform_items,
            "tasks": tasks_payload,
            "todos": [_todo_to_api(todo) for todo in quick_todos],
            "articles": articles,
            "account_crawling": normalize_account_crawling_settings(config, include_state=True),
            "recentEvents": recent_events,
            "pendingReviews": pending_reviews,
            "config": config,
            "profile": _build_snapshot_profile(profile_summary),
            "stats": _build_snapshot_stats(
                enabled_task_count=len(enabled_tasks),
                total_task_count=len(tasks),
                today_record_count=len(today_records),
                hit_record_count=hit_records,
                error_record_count=error_records,
            ),
            "monitoring": _build_snapshot_monitoring(
                enabled=monitoring_enabled,
                running=monitoring_running,
                status_message=monitoring_status,
            ),
            "historyStorage": get_structured_read_health(),
            "cloudSync": self._cloud_sync_manager.get_status(),
            "lastRun": self._last_run,
            "industryTags": tag_options["industryTags"],
            "regionTags": tag_options["regionTags"],
            "availableModels": available_models,
        }

    def trigger_run_all(self) -> dict:
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        if self._worker and self._worker.is_alive():
            return {"queued": False, "message": "已有任务正在执行"}

        def worker() -> None:
            with self._lock:
                self._last_run = {
                    "startedAt": _local_iso_seconds(),
                    "status": "running",
                    "items": [],
                }

            items: list[dict] = []
            session_manager = None
            try:
                config = self.load_config()
                default_notification = config.get("default_notification", {}) or {}
                global_mode = config.get("detection_mode", "browser")
                manual_tasks: list[dict] = []
                for task in config.get("tasks", []) or []:
                    if not task.get("enabled", True):
                        continue
                    manual_tasks.append(_apply_global_mode(task, global_mode))
                session_manager = self._create_query_session_manager(
                    manual_tasks,
                    config,
                    str(global_mode or "").strip(),
                    source_label="手动批量执行",
                )
            except Exception as exc:
                with self._lock:
                    self._last_run = {
                        "startedAt": self._last_run.get("startedAt") if self._last_run else _local_iso_seconds(),
                        "finishedAt": _local_iso_seconds(),
                        "status": "failed",
                        "items": [{"taskName": "初始化", "ok": False, "error": str(exc)}],
                    }
                return

            for task in manual_tasks:
                task_name = str(task.get("name") or derive_task_id(task)).strip()
                try:
                    result_items, report = run_task_group(
                        task,
                        default_notification,
                        config,
                        execution_source="manual",
                        return_report=True,
                        platform_session_manager=session_manager,
                    )
                    items.append(
                        {
                            "taskName": task_name,
                            "ok": True,
                            "queryCount": len(result_items),
                            "report": report,
                        }
                    )
                except Exception as exc:
                    items.append(
                        {
                            "taskName": task_name,
                            "ok": False,
                            "error": str(exc),
                        }
                    )

            try:
                if session_manager is not None:
                    session_manager.close_all(reason="手动批量执行结束")
            except Exception as exc:
                items.append({"taskName": "资源清理", "ok": False, "error": str(exc)})

            with self._lock:
                self._last_run = {
                    "startedAt": self._last_run.get("startedAt") if self._last_run else _local_iso_seconds(),
                    "finishedAt": _local_iso_seconds(),
                    "status": "done",
                    "items": items,
                }

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        return {"queued": True, "message": "已开始执行任务"}

    def save_task_config(self, payload: dict) -> dict:
        """保存任务配置（包括优化时间周期）。"""
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "message": "缺少 task_id"}
        with self._lock:
            config = self.load_config()
            tasks = config.get("tasks", []) or []
            target_task = None
            for task in tasks:
                if (task.get("task_id") or derive_task_id(task)) == task_id:
                    target_task = task
                    break

            if target_task is None:
                return {"ok": False, "message": f"未找到任务 {task_id}"}

            # 白名单字段更新
            allowed_fields = [
                "optimization_start_date", "optimization_end_date",
                "name", "webhook_url", "enabled",
            ]
            for field in allowed_fields:
                if field in payload:
                    target_task[field] = payload[field]
            if "recognition_batch_size" in payload:
                try:
                    target_task["recognition_batch_size"] = max(1, int(payload["recognition_batch_size"] or 1))
                except Exception:
                    pass
            if "fixed_screenshot_enabled" in payload:
                target_task["fixed_screenshot_enabled"] = bool(payload["fixed_screenshot_enabled"])
            if "extract_references_enabled" in payload:
                target_task["extract_references_enabled"] = bool(payload["extract_references_enabled"])
            if "fixed_screenshot_count" in payload:
                try:
                    target_task["fixed_screenshot_count"] = max(1, int(payload["fixed_screenshot_count"] or 1))
                except Exception:
                    pass

            # weekdays 单独处理（需要是 int 列表）
            if "weekdays" in payload:
                raw = payload["weekdays"]
                if isinstance(raw, list):
                    target_task["weekdays"] = [int(d) for d in raw if isinstance(d, (int, float))]

            self.save_config(config)
            self._invalidate_tasks_full_cache()
        self._refresh_monitoring_runtime(restart_scheduler=False)

        # 记录优化周期到 periods 历史
        task_name = str(target_task.get("name") or task_id).strip()
        start = str(payload.get("optimization_start_date") or "").strip()
        end = str(payload.get("optimization_end_date") or "").strip()
        if start and end:
            save_optimization_period(task_name, start, end)

        return {"ok": True}

    def save_profile(self, payload: dict) -> dict:
        """保存用户账号信息到 config.yaml 的 profile 段。"""
        cloud_profile_payload: dict[str, Any] = {}
        session = CloudSessionStore().load()
        session_user = session.get("user") if isinstance(session.get("user"), dict) else {}
        cloud_logged_in = bool(session.get("base_url") and session.get("access_token") and session.get("refresh_token") and session_user)
        if cloud_logged_in and isinstance(session_user, dict) and str(session_user.get("role") or "").strip() == "admin":
            if "name" in payload:
                cloud_profile_payload["display_name"] = str(payload.get("name") or "").strip() or None
            if "birthday" in payload:
                cloud_profile_payload["birthday"] = str(payload.get("birthday") or "").strip() or None
            if "hire_date" in payload:
                cloud_profile_payload["hire_date"] = str(payload.get("hire_date") or "").strip() or None

        with self._lock:
            config = self.load_config()
            profile = config.get("profile", {}) or {}

            allowed_fields = ["name", "role", "avatar", "birthday", "hire_date"]
            for field in allowed_fields:
                if field in payload:
                    value = str(payload[field]).strip()
                    if field == "avatar":
                        if is_profile_avatar_url(value):
                            continue
                        value = store_profile_avatar(value)
                    profile[field] = value

            config["profile"] = profile
            self.save_config(config)
            if cloud_logged_in and "avatar" in payload:
                raw_avatar = str(payload.get("avatar") or "").strip()
                if is_profile_avatar_url(raw_avatar):
                    portable_avatar = profile_avatar_data_url(profile.get("avatar", ""))
                    if portable_avatar:
                        cloud_profile_payload["avatar"] = portable_avatar
                    elif not profile.get("avatar"):
                        cloud_profile_payload["avatar"] = ""
                elif raw_avatar.startswith("user_data/profile/avatar"):
                    portable_avatar = profile_avatar_data_url(raw_avatar)
                    if portable_avatar:
                        cloud_profile_payload["avatar"] = portable_avatar
                else:
                    cloud_profile_payload["avatar"] = raw_avatar
        if cloud_profile_payload:
            store = CloudSessionStore()
            session_identity_key = cloud_session_identity_key(session)
            optimistic_user = dict(session_user)
            if "avatar" in cloud_profile_payload:
                optimistic_user["avatar"] = cloud_profile_payload.get("avatar") or None
            if str(optimistic_user.get("role") or "").strip() == "admin":
                if "display_name" in cloud_profile_payload:
                    optimistic_user["display_name"] = cloud_profile_payload.get("display_name")
                if "birthday" in cloud_profile_payload:
                    optimistic_user["birthday"] = cloud_profile_payload.get("birthday")
                if "hire_date" in cloud_profile_payload:
                    optimistic_user["hire_date"] = cloud_profile_payload.get("hire_date")
            try:
                store.update_user(optimistic_user)
            except Exception as exc:
                print(f"[WebBackend] 资料本地会话更新失败，将保留配置资料: {exc}")

            uploaded_immediately = False
            base_url = str(session.get("base_url") or "").strip()
            access_token = str(session.get("access_token") or "").strip()
            if base_url and access_token:
                try:
                    updated_user = SurfacedCloudClient(base_url, timeout_seconds=5.0).update_me_profile(
                        access_token,
                        cloud_profile_payload,
                    )
                    if isinstance(updated_user, dict) and cloud_session_identity_key(store.load()) == session_identity_key:
                        store.update_user(updated_user)
                        uploaded_immediately = True
                except Exception as exc:
                    print(f"[WebBackend] 资料云端即时同步失败，将转入后台补传: {exc}")

            if not uploaded_immediately:
                try:
                    enqueue_profile_update(cloud_profile_payload)
                except Exception as exc:
                    print(f"[WebBackend] 资料云端后台同步入队失败，将保留本地资料: {exc}")
        return {"ok": True, "cloud": self.get_cloud_status().get("cloud")}

    @staticmethod
    def get_public_profile(config: dict[str, Any]) -> dict[str, Any]:
        profile = dict(config.get("profile", {}) or {})
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        is_cloud_logged_in = bool(session.get("base_url") and session.get("access_token") and session.get("refresh_token") and user)
        if is_cloud_logged_in:
            cloud_avatar = str(user.get("avatar") or "").strip()
            cloud_role = str(user.get("role") or "").strip()
            is_admin = cloud_role == "admin"
            local_avatar = public_profile_avatar_url(profile.get("avatar", "")) if is_admin else ""
            cloud_name = str(
                (user.get("display_name") if is_admin else "")
                or user.get("username")
                or (profile.get("name") if is_admin else "")
                or ""
            ).strip()
            return {
                "name": cloud_name,
                "role": AppRuntime._cloud_profile_role_label(cloud_role),
                "avatar": public_profile_avatar_url(cloud_avatar) if cloud_avatar else local_avatar,
                "birthday": str(user.get("birthday") or (profile.get("birthday") if is_admin else "") or "").strip(),
                "hire_date": str(user.get("hire_date") or (profile.get("hire_date") if is_admin else "") or "").strip(),
            }
        profile["avatar"] = public_profile_avatar_url(profile.get("avatar", ""))
        return profile

    @staticmethod
    def _cloud_profile_role_label(role: Any) -> str:
        return {
            "admin": "管理",
            "operator": "运营",
            "viewer": "销售",
        }.get(str(role or "").strip(), "")

    def get_profile_avatar_asset(self) -> tuple[bytes, str] | None:
        return read_profile_avatar_asset(self.load_config())

    # ── Image generation ────────────────────────────────────────

    def get_image_generation_config(self) -> dict:
        config = self.load_config()
        return {
            "ok": True,
            "config": image_generation_config_to_api(
                (config.get("image_generation", {}) or {}),
                _mask_secret,
            ),
        }

    def generate_image(self, payload: dict) -> dict:
        config = self.load_config()
        try:
            return generate_image_from_config(config.get("image_generation", {}) or {}, payload)
        except ImageGenerationError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            print(f"[WebBackend] 生图请求失败: {redact_secret_text(str(exc))}")
            return {"ok": False, "message": "生图请求失败，请检查接口地址、Key、模型名和网络"}

    # ── Platform API Key management ──────────────────────────────

    @staticmethod
    def _platform_key_aliases(platform_id: str) -> list[str]:
        aliases: list[str] = []
        legacy_alias = _PLATFORM_ID_REVERSE.get(platform_id)
        if legacy_alias:
            aliases.append(legacy_alias)
        if platform_id == "tongyi":
            aliases.append("qwen")
        elif platform_id == "wenxin":
            aliases.append("ernie")
        return aliases

    def _platform_key_info(self, config: dict[str, Any], platform_id: str, platform_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        pcfg = platform_cfg if isinstance(platform_cfg, dict) else _get_platform_config_entry(config, platform_id)
        raw_key = str(pcfg.get("api_key", "") or "").strip()
        masked = ("*" * max(0, len(raw_key) - 4) + raw_key[-4:]) if len(raw_key) > 4 else ("*" * len(raw_key))
        return {
            "api_key_masked": masked,
            "has_key": bool(raw_key),
            "has_access": bool(raw_key) or platform_has_configured_access(config, platform_id),
            "requires_api_key": platform_requires_api_key(platform_id),
            "api_model": str(pcfg.get("api_model", "") or "").strip(),
            "api_fast_model": str(pcfg.get("api_fast_model", "") or "").strip(),
            "api_deep_model": str(pcfg.get("api_deep_model", "") or "").strip(),
            "model_options": _normalize_model_options(pcfg.get("model_options", [])),
            "api_test_status": _normalize_platform_test_status(pcfg.get("api_test_status")),
            "default_model": str((PLATFORM_API_CONFIG.get(platform_id) or {}).get("default_model") or "").strip(),
            "supports_split_models": False,
            "fast_model_default": "",
            "deep_model_default": "",
            "split_model_note": "",
        }

    def get_platform_keys(self) -> dict:
        """返回各平台 API Key（脱敏）和模型配置，供搜搜等功能使用。"""
        config = self.load_config()
        platforms_cfg = config.get("platforms", {}) or {}
        result: dict[str, dict[str, Any]] = {}

        for raw_pid, raw_pcfg in platforms_cfg.items():
            pid = _normalize_platform_id(str(raw_pid or "").strip())
            if not pid:
                continue
            entry = self._platform_key_info(config, pid, raw_pcfg if isinstance(raw_pcfg, dict) else {})
            result[pid] = entry
            for alias in self._platform_key_aliases(pid):
                result[alias] = entry

        for pid in PLATFORM_API_CONFIG:
            if pid in result:
                continue
            entry = self._platform_key_info(config, pid, {})
            result[pid] = entry
            for alias in self._platform_key_aliases(pid):
                result[alias] = entry

        return {"platforms": result}

    def save_platform_config(self, payload: dict) -> dict:
        """批量保存平台 API Key 和模型设置。"""
        with self._lock:
            config = self.load_config()
            platforms_cfg = config.get("platforms", {}) or {}
            updates = payload.get("platforms", {}) or {}
            if not isinstance(updates, dict):
                return {"ok": False, "message": "platforms 必须是对象"}
            for pid, raw_values in updates.items():
                values = raw_values if isinstance(raw_values, dict) else {}
                real_pid = _normalize_platform_id(str(pid or "").strip())
                if not real_pid:
                    continue
                if real_pid not in platforms_cfg or not isinstance(platforms_cfg.get(real_pid), dict):
                    platforms_cfg[real_pid] = {}
                if real_pid == "local_model":
                    platforms_cfg.pop("local_qwen", None)
                platform_cfg = platforms_cfg[real_pid]
                platform_changed = False

                if "api_key" in values:
                    raw_key = str(values.get("api_key", "") or "").strip()
                    if raw_key and not raw_key.startswith("*"):
                        existing_key = str(platform_cfg.get("api_key", "") or "").strip()
                        platform_cfg["api_key"] = raw_key
                        if raw_key != existing_key:
                            platform_changed = True

                for field in ("api_model", "api_fast_model", "api_deep_model"):
                    if field not in values:
                        continue
                    next_model = str(values.get(field, "") or "").strip()
                    if next_model != str(platform_cfg.get(field, "") or "").strip():
                        platform_changed = True
                    platform_cfg[field] = next_model

                if "model_options" in values:
                    next_options = _normalize_model_options(values.get("model_options", []))
                    if next_options != _normalize_model_options(platform_cfg.get("model_options", [])):
                        platform_changed = True
                    platform_cfg["model_options"] = next_options

                if platform_changed:
                    platform_cfg["api_test_status"] = "idle"

            config["platforms"] = platforms_cfg
            self.save_config(config)
        self._refresh_monitoring_runtime(restart_scheduler=False)
        self.start_account_crawl_scheduler()
        return {"ok": True}

    def test_platform(self, platform_id: str, payload: dict) -> dict:
        """测试某个平台的 API 连通性。"""
        import time as _time
        platform_id = _normalize_platform_id(platform_id)
        config = self.load_config()
        api_key = str(payload.get("api_key", "") or "").strip()
        model = str(payload.get("model", "") or "").strip()
        if not api_key or api_key.startswith("*"):
            api_key = get_platform_api_key(config, platform_id)
        if not model:
            pcfg = _get_platform_config_entry(config, platform_id)
            model = str(pcfg.get("api_model", "") or "").strip()
        if not model:
            model = str((PLATFORM_API_CONFIG.get(platform_id) or {}).get("default_model") or "").strip()
        if platform_requires_api_key(platform_id) and not api_key:
            return {"ok": False, "message": "请先填写 API Key"}
        if not model:
            return {"ok": False, "message": "请先填写模型名"}

        t0 = _time.monotonic()
        try:
            text = send_platform_chat_messages(
                platform_id,
                api_key,
                model,
                [
                    {"role": "system", "content": '你只需要回复"测试成功"。'},
                    {"role": "user", "content": '这是一条接口连通性测试。请只回复"测试成功"。'},
                ],
            )
            latency = int((_time.monotonic() - t0) * 1000)
            with self._lock:
                config = self.load_config()
                platforms_cfg = config.get("platforms", {}) or {}
                platform_cfg = platforms_cfg.get(platform_id, {})
                if not isinstance(platform_cfg, dict):
                    platform_cfg = {}
                    platforms_cfg[platform_id] = platform_cfg
                if platform_id == "local_model":
                    platforms_cfg.pop("local_qwen", None)
                platform_cfg["api_test_status"] = "success" if text else "error"
                config["platforms"] = platforms_cfg
                self.save_config(config)
            if not text:
                return {
                    "ok": False,
                    "message": get_platform_last_error(platform_id) or "接口已连通，但没有返回内容",
                    "latency_ms": latency,
                }
            return {"ok": True, "message": str(text).strip(), "latency_ms": latency}
        except Exception as exc:
            from ui.api_config import _classify_test_exception
            latency = int((_time.monotonic() - t0) * 1000)
            with self._lock:
                config = self.load_config()
                platforms_cfg = config.get("platforms", {}) or {}
                platform_cfg = platforms_cfg.get(platform_id, {})
                if not isinstance(platform_cfg, dict):
                    platform_cfg = {}
                    platforms_cfg[platform_id] = platform_cfg
                if platform_id == "local_model":
                    platforms_cfg.pop("local_qwen", None)
                platform_cfg["api_test_status"] = "error"
                config["platforms"] = platforms_cfg
                self.save_config(config)
            return {"ok": False, "message": _classify_test_exception(platform_id, exc), "latency_ms": latency}

    # ── Task CRUD ────────────────────────────────────────────────

    def get_tasks_full(self) -> dict:
        """返回所有任务的完整信息。"""
        self._process_pending_task_deletions()
        try:
            with self._lock:
                config = self.load_config()
                repaired = refresh_visible_cloud_task_day_statuses_from_history(config)
                if repaired:
                    self.save_config(config)
                    self._invalidate_tasks_full_cache()
        except Exception as exc:
            print(f"[WebBackend] 云端运行状态本地修复失败，将继续返回任务列表: {exc}")
        return self.task_overview_service.get_tasks_full()

    def get_deleted_tasks(self) -> dict:
        self._process_pending_task_deletions()
        with self._lock:
            config = self.load_config()
            changed = purge_expired_deleted_tasks(config)
            if changed:
                self.save_config(config)
            can_restore = self._current_cloud_role() == "admin"
            items = deleted_task_snapshots(config)
        return {
            "ok": True,
            "tasks": [
                {
                    **item,
                    "can_restore": bool(can_restore),
                }
                for item in items
            ],
            "retention_days": 3,
        }

    def restore_deleted_task(self, payload: dict[str, Any] | None = None) -> dict:
        request_payload = payload if isinstance(payload, dict) else {}
        if self._current_cloud_role() != "admin":
            return {"ok": False, "message": "只有管理员账号可以恢复已删除品牌配置"}

        deleted_task_id = str(request_payload.get("deleted_task_id") or request_payload.get("id") or "").strip()
        brand_name = str(request_payload.get("brand_name") or request_payload.get("brandName") or "").strip()
        with self._lock:
            config = self.load_config()
            changed = purge_expired_deleted_tasks(config)
            snapshots = deleted_task_snapshots(config)
            candidate = None
            for item in snapshots:
                if deleted_task_id and str(item.get("id") or "").strip() == deleted_task_id:
                    candidate = item
                    break
                if brand_name and str(item.get("brand") or item.get("name") or "").strip() == brand_name:
                    candidate = item
                    break
            if changed:
                self.save_config(config)
            if not candidate:
                return {"ok": False, "message": "未找到可恢复的品牌配置，可能已超过三天保留期"}

        cloud_ok, cloud_message, cloud_task = self._restore_cloud_deleted_task(candidate)
        if not cloud_ok:
            return {"ok": False, "message": cloud_message or "云端品牌配置恢复失败"}

        with self._lock:
            config = self.load_config()
            result = restore_deleted_task_backup(
                config,
                deleted_task_id=deleted_task_id or str(candidate.get("id") or ""),
                brand_name=brand_name,
            )
            if not result.get("ok"):
                return result
            restored_task = result.get("task") if isinstance(result.get("task"), dict) else {}
            if isinstance(cloud_task, dict) and restored_task:
                restored_task["cloud_task_id"] = _safe_int(cloud_task.get("id"), _safe_int(restored_task.get("cloud_task_id"), 0))
                restored_task["cloud_task_key"] = str(cloud_task.get("task_key") or restored_task.get("cloud_task_key") or "").strip()
                restored_task["cloud_config_version"] = _safe_int(
                    cloud_task.get("config_version"),
                    _safe_int(restored_task.get("cloud_config_version"), 1),
                )
                restored_task["cloud_access_level"] = "admin"
                if "enabled" in cloud_task:
                    restored_task["enabled"] = bool(cloud_task.get("enabled"))
            self.save_config(config)
            self._invalidate_tasks_full_cache()
            self._invalidate_article_cache()
        self._refresh_monitoring_runtime(restart_scheduler=False)
        return {
            "ok": True,
            "message": f"已恢复品牌配置「{restored_task.get('brand') or restored_task.get('name') or brand_name}」",
            "task_id": restored_task.get("task_id") or derive_task_id(restored_task),
        }

    def _current_cloud_role(self) -> str:
        session = CloudSessionStore().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        return str(user.get("role") or "").strip()

    def _is_cloud_viewer_account(self) -> bool:
        return self._current_cloud_role() == "viewer"

    def _viewer_execution_block_response(self, **extra: Any) -> dict[str, Any] | None:
        if not self._is_cloud_viewer_account():
            return None
        response = {
            "ok": False,
            "queued": False,
            "enabled": False,
            "message": "浏览账号仅可查看管理员分配的数据，不能运行任务",
        }
        response.update(extra)
        return response

    def _close_execution_runtime_for_viewer(self) -> None:
        if not self._is_cloud_viewer_account():
            return
        try:
            self.stop_monitoring(persist_preference=True)
        except Exception:
            pass
        try:
            self._stop_recognition_test_session(restore_previous=False)
        except Exception:
            pass
        manager = self._recognition_manager
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                pass

    def _task_is_formal_running(self, task: dict[str, Any]) -> bool:
        try:
            status = get_task_day_status(task)
            if bool(status.get("formal_running")):
                return True
        except Exception:
            pass

        scheduler = self._scheduler
        if scheduler is None:
            return False
        task_id = str(task.get("task_id") or derive_task_id(task)).strip()
        task_name = str(task.get("name") or task_id).strip()
        try:
            running_ids = set(str(item) for item in (scheduler.get_running_task_ids() or []))
            if task_id in running_ids or any(item.startswith(f"{task_id}::") for item in running_ids):
                return True
        except Exception:
            pass
        try:
            running_tasks = dict(scheduler.get_running_tasks() or {})
            return any(key == task_name or key.startswith(f"{task_name} [") for key in running_tasks)
        except Exception:
            return False

    def _delete_cloud_task_for_local_task(self, task: dict[str, Any]) -> tuple[bool, str]:
        cloud_task_id = _safe_int(task.get("cloud_task_id") or task.get("cloudTaskId"), 0)
        if cloud_task_id <= 0:
            return True, ""
        if self._current_cloud_role() != "admin":
            return False, "只有管理员账号可以删除云端品牌任务"
        try:
            self._cloud_runtime_command("cloud.flush_outbox", {"limit": 10000})
        except Exception as exc:
            print(f"[WebBackend] 删除任务前上传云端 outbox 失败，将继续尝试删除: {exc}")
        ok, _payload, message = self._cloud_runtime_payload_command("cloud.delete_admin_task", {"task_id": cloud_task_id})
        if not ok:
            return False, message or "云端任务删除失败"
        return True, ""

    def _restore_cloud_deleted_task(self, deleted_task: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        cloud_task_id = _safe_int(deleted_task.get("cloud_task_id"), 0)
        if cloud_task_id <= 0:
            return True, "", {}
        if self._current_cloud_role() != "admin":
            return False, "只有管理员账号可以恢复云端品牌任务", {}
        ok, task, message = self._cloud_runtime_payload_command(
            "cloud.restore_admin_task",
            {"task_id": cloud_task_id},
        )
        if not ok:
            return False, message or "云端任务恢复失败", {}
        return True, "", task if isinstance(task, dict) else {}

    def _process_pending_task_deletions(self) -> None:
        if not self._pending_delete_processing_lock.acquire(blocking=False):
            return
        try:
            pending_tasks: list[dict[str, Any]] = []
            with self._lock:
                config = self.load_config()
                changed = purge_expired_deleted_tasks(config)
                for task in (config.get("tasks") or []):
                    if isinstance(task, dict) and bool(task.get("delete_pending")) and not self._task_is_formal_running(task):
                        pending_tasks.append(copy.deepcopy(task))
                if changed:
                    self.save_config(config)

            for task in pending_tasks:
                deleted_any = False
                task_id = str(task.get("task_id") or derive_task_id(task)).strip()
                pending_reason = str(task.get("delete_pending_reason") or "").strip()
                cloud_already_deleted = pending_reason == "cloud_deleted"
                if cloud_already_deleted:
                    cloud_ok, cloud_message = True, ""
                else:
                    cloud_ok, cloud_message = self._delete_cloud_task_for_local_task(task)
                with self._lock:
                    config = self.load_config()
                    tasks = [item for item in (config.get("tasks") or []) if isinstance(item, dict)]
                    target = next(
                        (
                            item
                            for item in tasks
                            if str(item.get("task_id") or derive_task_id(item)).strip() == task_id
                        ),
                        None,
                    )
                    if not target:
                        continue
                    if self._task_is_formal_running(target):
                        continue
                    if not cloud_ok:
                        target["delete_pending_error"] = cloud_message or "云端任务删除失败，稍后会重试"
                        config["tasks"] = tasks
                        self.save_config(config)
                        self._invalidate_tasks_full_cache()
                        continue
                    tombstone = soft_delete_task(
                        config,
                        task_id,
                        source="cloud" if cloud_already_deleted else "local",
                        reason="cloud_deleted_after_formal_run" if cloud_already_deleted else "pending_formal_run_completed",
                        deleted_at=str(task.get("cloud_deleted_at") or "") if cloud_already_deleted else None,
                        expires_at=str(task.get("cloud_delete_expires_at") or task.get("delete_pending_expires_at") or "") if cloud_already_deleted else None,
                    )
                    if tombstone:
                        self.save_config(config)
                        self._invalidate_tasks_full_cache()
                        self._invalidate_article_cache()
                        deleted_any = True
                        print(f"[WebBackend] 已在正式任务结束后软删除品牌任务: {tombstone.get('name') or task_id}")
                if deleted_any:
                    self._refresh_monitoring_runtime(restart_scheduler=False)
        finally:
            self._pending_delete_processing_lock.release()

    def get_task_trend(self, task_id: str, range_key: str) -> dict:
        config = self.load_config()
        for task in (config.get("tasks", []) or []):
            current_id = task.get("task_id") or derive_task_id(task)
            if current_id != task_id:
                continue
            return _build_task_trend(task, range_key)
        return _empty_trend_payload(range_key)

    def get_dashboard_trend(self, range_key: str) -> dict:
        config = self.load_config()
        tasks = list(config.get("tasks", []) or [])
        enabled_tasks = [task for task in tasks if task.get("enabled", True)]
        return _build_dashboard_trend(enabled_tasks, range_key)

    @staticmethod
    def _xml_text(node: ElementTree.Element | None, tag: str, default: str = "") -> str:
        if node is None:
            return default
        direct = node.find(tag)
        if direct is not None and direct.text:
            return str(direct.text or "").strip()
        for child in list(node):
            if str(child.tag or "").split("}")[-1] == tag and child.text:
                return str(child.text or "").strip()
        return default

    @staticmethod
    def _parse_feed_datetime(value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return parsedate_to_datetime(text)
        except Exception:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None

    @staticmethod
    def _feed_datetime_local_date(value: str) -> date | None:
        parsed = AppRuntime._parse_feed_datetime(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            return parsed.date()
        tz = local_now().tzinfo
        return parsed.astimezone(tz).date() if tz is not None else parsed.astimezone().date()

    @staticmethod
    def _format_feed_datetime(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        parsed = AppRuntime._parse_feed_datetime(text)
        if parsed is None:
            return text
        if parsed.tzinfo is None:
            return parsed.strftime("%m月%d日 %H:%M")
        tz = local_now().tzinfo
        localized = parsed.astimezone(tz) if tz is not None else parsed.astimezone()
        return localized.strftime("%m月%d日 %H:%M")

    @staticmethod
    def _strip_feed_html(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
        text = re.sub(r"(?i)</\s*(p|div|li|h[1-6]|blockquote)\s*>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _aihot_ssl_context() -> ssl.SSLContext | None:
        if certifi is None:
            return None
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return None

    def _parse_aihot_daily_json(self, data: Any) -> dict:
        if not isinstance(data, dict):
            raise ValueError("日报接口返回格式异常")

        feed_date = str(data.get("date") or "").strip()
        updated_raw = str(data.get("generatedAt") or data.get("windowEnd") or "").strip()
        items: list[dict[str, str]] = []

        for section in data.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_label = str(section.get("label") or "").strip()
            for entry in section.get("items") or []:
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title") or "").strip()
                summary = str(entry.get("summary") or "").strip()
                link = str(entry.get("sourceUrl") or entry.get("url") or "").strip()
                source_name = str(entry.get("sourceName") or "").strip()
                if not title:
                    continue
                items.append(
                    {
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "content": summary,
                        "author": source_name or section_label,
                        "publishedAt": "",
                    }
                )

        return {
            "ok": True,
            "title": "AI 热点日报",
            "feedUrl": AIHOT_DAILY_PUBLIC_URL,
            "updatedAt": self._format_feed_datetime(updated_raw),
            "date": feed_date,
            "items": items,
        }

    def _parse_aihot_daily_feed(self, xml_text: str) -> dict:
        root = ElementTree.fromstring(xml_text.encode("utf-8"))
        channel = root.find("channel")
        if channel is None and str(root.tag or "").split("}")[-1] == "channel":
            channel = root
        title = self._xml_text(channel, "title", "AI 热点日报")
        updated_at = self._format_feed_datetime(
            self._xml_text(channel, "lastBuildDate") or self._xml_text(channel, "pubDate")
        )
        items: list[dict[str, str]] = []
        item_nodes = channel.findall("item") if channel is not None else []
        if not item_nodes:
            item_nodes = [node for node in root.iter() if str(node.tag or "").split("}")[-1] in {"item", "entry"}]
        today = local_today()
        for node in item_nodes:
            link = self._xml_text(node, "link")
            if not link:
                link_node = node.find("link")
                link = str(link_node.attrib.get("href") or "").strip() if link_node is not None else ""
            published_raw = self._xml_text(node, "pubDate") or self._xml_text(node, "updated")
            published_date = self._feed_datetime_local_date(published_raw)
            if published_date != today:
                continue
            content = self._strip_feed_html(
                self._xml_text(node, "encoded")
                or self._xml_text(node, "content")
                or self._xml_text(node, "description")
                or self._xml_text(node, "summary")
            )
            items.append(
                {
                    "title": self._xml_text(node, "title", "未命名热点"),
                    "link": link,
                    "summary": content,
                    "content": content,
                    "author": self._xml_text(node, "author"),
                    "publishedAt": self._format_feed_datetime(published_raw),
                }
            )
        return {
            "ok": True,
            "title": title or "AI 热点日报",
            "feedUrl": AIHOT_DAILY_FEED_URL,
            "updatedAt": updated_at,
            "items": items,
        }

    def _fetch_aihot_daily_rss_feed(self) -> dict:
        request = Request(
            AIHOT_DAILY_FEED_URL,
            headers={
                "User-Agent": f"{APP_NAME}/1.0 (+https://localhost; RSS reader)",
                "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
            },
        )
        with urlopen(request, timeout=8, context=self._aihot_ssl_context()) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            if "xml" not in content_type.lower():
                raise ValueError(f"订阅源返回了非 RSS 内容：{content_type or '未知内容类型'}")
            xml_text = response.read(AIHOT_DAILY_FEED_MAX_BYTES).decode("utf-8", errors="replace")
        return self._parse_aihot_daily_feed(xml_text)

    def get_aihot_daily_feed(self) -> dict:
        now = time.time()
        with self._aihot_daily_feed_lock:
            if self._aihot_daily_feed_cache and now - self._aihot_daily_feed_cached_at < AIHOT_DAILY_FEED_CACHE_SECONDS:
                return dict(self._aihot_daily_feed_cache)
            cached_payload = dict(self._aihot_daily_feed_cache) if self._aihot_daily_feed_cache else None
            etag = self._aihot_daily_feed_etag
            last_modified = self._aihot_daily_feed_last_modified
        try:
            headers = {
                "User-Agent": f"{APP_NAME}/1.0 (+https://localhost; daily digest reader)",
                "Accept": "application/json, */*;q=0.5",
            }
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            request = Request(
                AIHOT_DAILY_PUBLIC_URL,
                headers=headers,
            )
            with urlopen(request, timeout=8, context=self._aihot_ssl_context()) as response:
                content_type = str(response.headers.get("Content-Type") or "")
                if "json" not in content_type.lower():
                    raise ValueError(f"日报接口返回了非 JSON 内容：{content_type or '未知内容类型'}")
                raw_text = response.read(AIHOT_DAILY_FEED_MAX_BYTES).decode("utf-8", errors="replace")
                response_etag = str(response.headers.get("ETag") or "").strip()
                response_last_modified = str(response.headers.get("Last-Modified") or "").strip()
            payload = self._parse_aihot_daily_json(json.loads(raw_text))
            with self._aihot_daily_feed_lock:
                self._aihot_daily_feed_etag = response_etag
                self._aihot_daily_feed_last_modified = response_last_modified
        except HTTPError as exc:
            if cached_payload:
                payload = cached_payload
            else:
                try:
                    payload = self._fetch_aihot_daily_rss_feed()
                except Exception:
                    payload = {
                        "ok": False,
                        "title": "AI 热点日报",
                        "feedUrl": AIHOT_DAILY_PUBLIC_URL,
                        "updatedAt": "",
                        "items": [],
                        "message": f"日报读取失败：HTTP {exc.code}",
                    }
        except Exception as exc:
            if cached_payload:
                payload = cached_payload
            else:
                try:
                    payload = self._fetch_aihot_daily_rss_feed()
                except Exception:
                    payload = {
                        "ok": False,
                        "title": "AI 热点日报",
                        "feedUrl": AIHOT_DAILY_PUBLIC_URL,
                        "updatedAt": "",
                        "items": [],
                        "message": f"日报读取失败：{exc}",
                    }
        with self._aihot_daily_feed_lock:
            if payload.get("ok"):
                self._aihot_daily_feed_cache = dict(payload)
                self._aihot_daily_feed_cached_at = time.time()
        return payload

    def get_task_monthly_stats(self, task_id: str) -> dict:
        """返回某任务过去6个月每月的文章数，用于品牌柱状图。"""
        # 找到 task_name
        config = self.load_config()
        tasks = list(config.get("tasks", []) or [])
        task_name = ""
        for task in tasks:
            tid = task.get("task_id") or derive_task_id(task)
            if tid == task_id:
                task_name = str(task.get("name") or tid).strip()
                break
        if not task_name:
            return {"months": []}

        try:
            articles = self._get_synced_articles(config)
            task_articles = [a for a in articles if task_name in (a.get("matched_tasks") or [])]
        except Exception:
            task_articles = []

        # 按月统计文章数，过去6个月
        today = local_today()
        months = []
        for i in range(5, -1, -1):
            m = today.month - i
            y = today.year
            while m <= 0:
                m += 12
                y -= 1
            month_str = f"{y:04d}-{m:02d}"
            label = f"{m}月"
            auth = sum(
                1 for a in task_articles
                if str(a.get("ts", "")).startswith(month_str) and str(a.get("media_type", "")).strip() == "authority"
            )
            self_ = sum(
                1 for a in task_articles
                if str(a.get("ts", "")).startswith(month_str) and str(a.get("media_type", "")).strip() != "authority"
            )
            months.append({"name": label, "value": auth + self_, "auth": auth, "self": self_})
        return {"months": months}

    def get_task_article_reference_ranking(
        self,
        task_id: str,
        *,
        platform: str = "all",
        date_from: str = "",
        date_to: str = "",
    ) -> dict:
        return self.article_reference_service.get_task_article_reference_ranking(
            task_id,
            platform=platform,
            date_from=date_from,
            date_to=date_to,
        )

    def create_task(self, payload: dict) -> dict:
        """创建新任务。"""
        with self._lock:
            config = self.load_config()
            tasks = config.get("tasks", []) or []
            new_task = self._build_task_from_payload(payload)
            if not str(new_task.get("webhook_url", "") or "").strip():
                new_task["webhook_url"] = get_most_common_task_webhook(tasks)
            if "recognition_batch_size" not in payload:
                new_task["recognition_batch_size"] = compute_recognition_batch_size_from_keywords(
                    list(new_task.get("keywords", []) or []),
                    fallback_platforms=list(new_task.get("platforms", []) or []),
                )
            if not str(new_task.get("name", "") or "").strip():
                return {"ok": False, "message": "任务名称不能为空"}
            from core.daily_task_state import assign_task_id
            assign_task_id(new_task)
            if not str(new_task.get("created_at") or "").strip():
                new_task["created_at"] = local_now().isoformat(timespec="seconds")
            tasks.append(new_task)
            config["tasks"] = tasks
            self.save_config(config)
            task_id = str(new_task.get("task_id", "") or "").strip()
            base_snapshot = self._get_cached_task_snapshot(task_id)
            snapshot_config = copy.deepcopy(config)
            snapshot_task = copy.deepcopy(new_task)
            self._invalidate_tasks_full_cache()
            self._invalidate_article_cache()
        self._refresh_monitoring_runtime()
        return {
            "ok": True,
            "task_id": task_id,
            "task": self._get_light_task_snapshot(
                task_id,
                config=snapshot_config,
                task=snapshot_task,
                base_snapshot=base_snapshot,
            ),
        }

    def update_task(self, task_id: str, payload: dict) -> dict:
        """合并更新任务（只覆盖 payload 中存在的字段）。"""
        with self._lock:
            config = self.load_config()
            tasks = config.get("tasks", []) or []
            target_idx = None
            for i, task in enumerate(tasks):
                if (task.get("task_id") or derive_task_id(task)) == task_id:
                    target_idx = i
                    break
            if target_idx is None:
                return {"ok": False, "message": f"未找到任务 {task_id}"}
            existing = dict(tasks[target_idx])
            partial = self._build_task_from_payload(payload)
            if "webhook_url" in payload:
                incoming_webhook = str(payload.get("webhook_url", "") or "").strip()
                existing_webhook = str(existing.get("webhook_url", "") or "").strip()
                if not incoming_webhook and existing_webhook:
                    partial.pop("webhook_url", None)
            existing.update(partial)
            existing["task_id"] = task_id
            tasks[target_idx] = existing
            config["tasks"] = tasks
            self.save_config(config)
            base_snapshot = self._get_cached_task_snapshot(task_id)
            snapshot_config = copy.deepcopy(config)
            snapshot_task = copy.deepcopy(existing)
            self._invalidate_tasks_full_cache()
            self._invalidate_article_cache()
        self._refresh_monitoring_runtime()
        return {
            "ok": True,
            "task": self._get_light_task_snapshot(
                task_id,
                config=snapshot_config,
                task=snapshot_task,
                base_snapshot=base_snapshot,
            ),
        }

    def delete_task(self, task_id: str) -> dict:
        """软删除任务，保留三天可恢复备份。"""
        if self._current_cloud_role() != "admin":
            return {"ok": False, "message": "只有管理员账号可以删除品牌任务"}

        target_snapshot: dict[str, Any] | None = None
        with self._lock:
            config = self.load_config()
            tasks = config.get("tasks", []) or []
            target = next(
                (
                    t
                    for t in tasks
                    if isinstance(t, dict) and (t.get("task_id") or derive_task_id(t)) == task_id
                ),
                None,
            )
            if target is None:
                return {"ok": False, "message": f"未找到任务 {task_id}"}
            if self._task_is_formal_running(target):
                mark_task_delete_pending(target, reason="formal_running")
                config["tasks"] = tasks
                self.save_config(config)
                self._invalidate_tasks_full_cache()
                return {
                    "ok": True,
                    "pending": True,
                    "message": "当前正式任务正在运行，已记录删除请求；运行结束并同步数据后会自动软删除。",
                }
            target_snapshot = copy.deepcopy(target)

        cloud_ok, cloud_message = self._delete_cloud_task_for_local_task(target_snapshot or {})
        if not cloud_ok:
            return {"ok": False, "message": cloud_message or "云端任务删除失败"}

        with self._lock:
            config = self.load_config()
            tasks = [item for item in (config.get("tasks") or []) if isinstance(item, dict)]
            target = next(
                (
                    item
                    for item in tasks
                    if str(item.get("task_id") or derive_task_id(item)).strip() == task_id
                ),
                None,
            )
            if target is None:
                return {"ok": False, "message": f"未找到任务 {task_id}"}
            if self._task_is_formal_running(target):
                mark_task_delete_pending(target, reason="formal_running")
                config["tasks"] = tasks
                self.save_config(config)
                self._invalidate_tasks_full_cache()
                return {
                    "ok": True,
                    "pending": True,
                    "message": "当前正式任务已开始运行，云端已记录删除请求；运行结束并同步数据后会自动软删除。",
                }
            tombstone = soft_delete_task(
                config,
                task_id,
                source="local",
                reason="manual_admin_delete",
            )
            if not tombstone:
                return {"ok": False, "message": f"未找到任务 {task_id}"}
            self.save_config(config)
            self._invalidate_tasks_full_cache()
            self._invalidate_article_cache()
        self._refresh_monitoring_runtime(restart_scheduler=False)
        return {
            "ok": True,
            "message": f"已删除品牌任务「{tombstone.get('name') or task_id}」，备份将保留三天。",
            "deleted_task": {key: tombstone.get(key) for key in ("id", "task_id", "name", "brand", "deleted_at", "expires_at")},
        }

    def test_run_task(self, task_id: str) -> dict:
        """单任务测试运行（同步，阻塞直到完成）。"""
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        config = self.load_config()
        target_task = None
        for task in config.get("tasks", []) or []:
            if (task.get("task_id") or derive_task_id(task)) == task_id:
                target_task = task
                break
        if not target_task:
            return {"ok": False, "message": f"未找到任务 {task_id}"}

        # 用全局检测模式覆盖任务的 mode
        global_mode = config.get("detection_mode", "browser")
        target_task = _apply_global_mode(target_task, global_mode)

        # 预检：任务是否有可执行的关键词
        keywords = target_task.get("keywords") or []
        executable = [
            kw for kw in keywords
            if kw.get("keyword", "").strip()
            and kw.get("platforms")
        ]
        if not executable:
            if global_mode == "recognition":
                return {"ok": False, "message": "当前检测模式为识别模式，不支持主动测试运行。请截图后粘贴触发识别。"}
            return {"ok": False, "message": "该任务没有可执行的关键词或平台，请先在任务编辑页配置关键词和平台。"}

        default_notification = config.get("default_notification", {}) or {}
        task_name = target_task.get("name", task_id)
        print(f"[WebBackend] 手动测试运行: 任务={task_name}, 模式={global_mode}, 可执行查询数={len(executable)}")
        for kw in executable:
            print(
                f"[WebBackend] 测试任务明细: 任务={task_name}, "
                f"关键词={str(kw.get('keyword', '')).strip()}, "
                f"品牌={str(kw.get('brand', '')).strip() or str(target_task.get('brand', '')).strip()}, "
                f"模式={str(kw.get('mode', 'browser')).strip()}, "
                f"平台={list(kw.get('platforms') or [])}"
            )
        session_manager = self._create_query_session_manager(
            [target_task],
            config,
            str(global_mode or "").strip(),
            source_label="手动测试",
        )
        try:
            result_items, report = run_task_group(
                target_task, default_notification, config,
                force_notify=True, execution_source="manual_test", return_report=True,
                platform_session_manager=session_manager,
            )
            hit = sum(1 for r in result_items if r.get("rank", 99) != 99)
            print(f"[WebBackend] 测试完成: 任务={task_name}, 总查询={len(result_items)}, 命中={hit}")
            task_success = is_report_success(report)
            msg = f"测试完成：共查询 {len(result_items)} 次，命中 {hit} 次"
            if hit == 0 and len(result_items) > 0:
                errors = [r.get("error_message", "") for r in result_items if r.get("error_message")]
                if errors:
                    msg += f"。错误：{errors[0]}"
            if task_success:
                self._clear_test_failure_notice(task_id)
            else:
                task_day_status = get_task_day_status(target_task)
                if str(task_day_status.get("brand_status") or "").strip() in {"success", "sent"}:
                    self._clear_test_failure_notice(task_id)
                else:
                    self._set_test_failure_notice(
                        task_id,
                        resolve_report_display_message(report),
                    )
            return {"ok": True, "queryCount": len(result_items), "hitCount": hit, "message": msg, "report": report}
        except Exception as exc:
            print(f"[WebBackend] 测试运行异常: 任务={task_name}, {exc}")
            return {"ok": False, "message": str(exc)}
        finally:
            if session_manager is not None:
                session_manager.close_all(reason="手动测试结束")

    def _set_test_run_state(self, run_id: str, patch: dict[str, Any]) -> None:
        self._test_run_state.update(run_id, patch)

    def _prune_test_runs_locked(self) -> None:
        self._test_run_state.prune_terminal()

    def _prune_search_file_caches_locked(self) -> None:
        now = local_now()
        for cache, timestamp_key in (
            (self._search_uploads, "uploaded_at"),
            (self._search_outputs, "created_at"),
        ):
            expired: list[str] = []
            for item_id, item in list(cache.items()):
                path = Path(str((item or {}).get("path") or ""))
                age = _runtime_timestamp_age_seconds((item or {}).get(timestamp_key), now=now)
                if not path.exists() or (age is not None and age > SEARCH_FILE_CACHE_TTL_SECONDS):
                    expired.append(item_id)
            for item_id in expired:
                cache.pop(item_id, None)

    def _resolve_test_run_failure_context(
        self,
        run_id: str,
        report: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        state = self._test_run_state.get(run_id)
        failed_details = list((report or {}).get("failed_query_details") or [])
        failure_entry = dict(failed_details[0] or {}) if failed_details else {}
        keyword = str(
            failure_entry.get("keyword")
            or state.get("currentKeyword")
            or ""
        ).strip()
        platform = str(
            failure_entry.get("platform")
            or state.get("currentPlatform")
            or ""
        ).strip()
        error_message = str(
            failure_entry.get("error_message")
            or state.get("errorMessage")
            or (report or {}).get("task_failure_message")
            or ""
        ).strip()
        return keyword, platform, error_message

    @staticmethod
    def _build_test_run_failure_details(report: dict[str, Any] | None = None) -> list[dict[str, str]]:
        failed_details = []
        for item in list((report or {}).get("failed_query_details") or []):
            if not isinstance(item, dict):
                continue
            failed_details.append({
                "keyword": str(item.get("keyword") or "").strip(),
                "platform": str(item.get("platform") or "").strip(),
                "brand": str(item.get("brand") or "").strip(),
                "mode": str(item.get("mode") or "").strip(),
                "errorMessage": str(item.get("error_message") or "").strip(),
                "failureType": str(item.get("failure_type") or "").strip(),
            })
        return failed_details

    def _build_test_run_force_send_snapshot(
        self,
        task: dict[str, Any] | None = None,
        *,
        task_id: str = "",
    ) -> dict[str, Any]:
        target_task = task
        resolved_id = str(task_id or "").strip()
        if target_task is None:
            config = self.load_config()
            _, located_task, located_id = self._locate_task(config, task_id=resolved_id)
            target_task = located_task
            resolved_id = located_id or resolved_id
        if not target_task:
            return {
                "sendableSuccessCount": 0,
                "actualScreenshotCount": 0,
                "canForceSendSuccess": False,
            }
        payload = _collect_today_successful_task_payload(target_task)
        screenshot_paths = [
            str(item or "").strip()
            for item in list(payload.get("screenshotPaths") or [])
            if str(item or "").strip()
        ]
        actual_screenshot_count = max(
            len(screenshot_paths),
            int(payload.get("actualScreenshotCount") or 0),
        )
        return {
            "sendableSuccessCount": len(screenshot_paths),
            "actualScreenshotCount": actual_screenshot_count,
            "canForceSendSuccess": bool(screenshot_paths),
        }

    def cancel_test_run(self, run_id: str) -> dict:
        with self._test_run_lock:
            state = self._test_run_state.get(run_id)
            if not state:
                return {"ok": False, "message": "未找到测试任务"}
            status = str(state.get("status") or "").strip()
            if status in {"success", "failed", "cancelled"}:
                return {
                    "ok": True,
                    "runId": run_id,
                    "status": status,
                    "message": str(state.get("message") or "测试任务已结束").strip() or "测试任务已结束",
                    "errorMessage": str(state.get("errorMessage") or "").strip(),
                }
            cancel_event = self._test_run_state.get_cancel_event(run_id)
            if cancel_event is None:
                return {"ok": False, "message": "当前测试任务不支持中断"}
            cancel_event.set()
            self._test_run_state.update(run_id, {
                "cancelRequested": True,
                "message": "正在中断测试任务...",
            })
            return {
                "ok": True,
                "runId": run_id,
                "status": status or "running",
                "message": "正在中断测试任务...",
            }

    def _get_active_test_run_block_reason_locked(self) -> str:
        self._prune_test_runs_locked()
        if self._test_run_state.active_run_ids():
            return "当前已有进行中的测试任务，请等待结束后再启动新的测试任务。"
        if self._test_run_state.has_cancelling_run():
            return "当前测试任务正在中断中，请稍后再试。"
        return ""

    def _get_test_run_block_reason(self) -> str:
        scheduler = self._scheduler
        if scheduler is not None:
            try:
                running_tasks = dict(scheduler.get_running_tasks() or {})
            except Exception:
                running_tasks = {}
            if running_tasks:
                return "当前已有正式任务运行中，请等待结束后再启动测试任务。"

        manager = self._recognition_test_manager
        if manager is not None and hasattr(manager, "get_runtime_status"):
            try:
                runtime_status = manager.get_runtime_status() or {}
            except Exception:
                runtime_status = {}
            if bool(runtime_status.get("running")):
                return "当前已有进行中的识别测试，请等待结束后再启动新的测试任务。"

        with self._test_run_lock:
            return self._get_active_test_run_block_reason_locked()

    def start_test_run_task(self, task_id: str) -> dict:
        """异步启动单任务测试运行，返回 run_id 给前端轮询。"""
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        blocked_reason = self._get_test_run_block_reason()
        if blocked_reason:
            return {"ok": False, "message": blocked_reason}
        config = self.load_config()
        target_task = None
        for task in config.get("tasks", []) or []:
            if (task.get("task_id") or derive_task_id(task)) == task_id:
                target_task = task
                break
        if not target_task:
            return {"ok": False, "message": f"未找到任务 {task_id}"}

        global_mode = config.get("detection_mode", "browser")
        if str(global_mode or "").strip() == "recognition":
            keywords = target_task.get("keywords") or []
            executable = [
                kw for kw in keywords
                if kw.get("keyword", "").strip()
                and kw.get("platforms")
            ]
            if not executable:
                return {"ok": False, "message": "该任务没有可用于识别模式测试的关键词或平台，请先在任务编辑页配置关键词和平台。"}
            return self._start_recognition_test_session(target_task)
        target_task = _apply_global_mode(target_task, global_mode)
        keywords = target_task.get("keywords") or []
        executable = [
            kw for kw in keywords
            if kw.get("keyword", "").strip()
            and kw.get("platforms")
        ]
        if not executable:
            if global_mode == "recognition":
                return {"ok": False, "message": "当前检测模式为识别模式，不支持主动测试运行。请截图后粘贴触发识别。"}
            return {"ok": False, "message": "该任务没有可执行的关键词或平台，请先在任务编辑页配置关键词和平台。"}

        run_id = uuid4().hex
        task_name = str(target_task.get("name") or task_id).strip()
        default_notification = config.get("default_notification", {}) or {}
        initial_state = {
            "runId": run_id,
            "taskId": task_id,
            "taskName": task_name,
            "status": "queued",
            "currentQuery": 0,
            "completedQueries": 0,
            "totalQueries": sum(len(kw.get("platforms") or []) for kw in executable),
            "hitQueries": 0,
            "currentAttempt": 0,
            "totalAttempts": 0,
            "currentKeyword": "",
            "currentPlatform": "",
            "message": "测试准备中",
            "result": "",
            "startedAt": _local_iso_seconds(),
            "updatedAt": _local_iso_seconds(),
            "finishedAt": "",
            "errorMessage": "",
            "failureDetails": [],
            "pollDiagnostics": [],
            "cancelRequested": False,
            "sendableSuccessCount": 0,
            "actualScreenshotCount": 0,
            "canForceSendSuccess": False,
        }
        cancel_event = threading.Event()
        with self._test_run_lock:
            blocked_reason = self._get_active_test_run_block_reason_locked()
            if blocked_reason:
                return {"ok": False, "message": blocked_reason}
            self._test_run_state.create(run_id, initial_state, cancel_event=cancel_event)

        def _on_progress(payload: dict[str, Any]) -> None:
            stage = str(payload.get("stage") or "").strip()
            if stage == "started":
                self._set_test_run_state(run_id, {
                    "status": "running",
                    "message": "测试已开始",
                })
                return
            if stage == "query_start":
                current_query = int(payload.get("current_query") or 0)
                total_queries = int(payload.get("total_queries") or initial_state["totalQueries"])
                self._set_test_run_state(run_id, {
                    "status": "running",
                    "currentQuery": current_query,
                    "completedQueries": int(payload.get("completed_queries") or 0),
                    "totalQueries": total_queries,
                    "hitQueries": int(payload.get("hit_queries") or 0),
                    "currentAttempt": 0,
                    "totalAttempts": 0,
                    "currentKeyword": str(payload.get("keyword") or "").strip(),
                    "currentPlatform": str(payload.get("platform") or "").strip(),
                    "message": f"正在执行第 {current_query} / {total_queries} 次测试",
                })
                return
            if stage == "attempt_start":
                attempt = int(payload.get("attempt") or 0)
                max_attempts = int(payload.get("max_attempts") or 0)
                self._set_test_run_state(run_id, {
                    "status": "running",
                    "currentAttempt": attempt,
                    "totalAttempts": max_attempts,
                    "currentKeyword": str(payload.get("keyword") or "").strip(),
                    "currentPlatform": str(payload.get("platform") or "").strip(),
                    "message": f"正在执行第 {attempt} / {max_attempts} 次测试",
                })
                return
            if stage == "attempt_done":
                succeeded = bool(payload.get("success"))
                self._set_test_run_state(run_id, {
                    "status": "running",
                    "currentAttempt": int(payload.get("attempt") or 0),
                    "totalAttempts": int(payload.get("max_attempts") or 0),
                    "currentKeyword": str(payload.get("keyword") or "").strip(),
                    "currentPlatform": str(payload.get("platform") or "").strip(),
                    "message": "本次命中目标" if succeeded else "本次未命中目标",
                    "errorMessage": str(payload.get("error_message") or "").strip(),
                })
                return
            if stage == "browser_poll_diagnostics":
                raw_metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
                metrics = dict(raw_metrics or {})
                item = {
                    "platform": str(payload.get("platform") or metrics.get("platform") or "").strip(),
                    "outcome": str(metrics.get("outcome") or "").strip(),
                    "pollSchedules": int(metrics.get("poll_schedules") or 0),
                    "fullTextReads": int(metrics.get("full_text_reads") or 0),
                    "fullTextSkips": int(metrics.get("full_text_read_skips") or 0),
                    "answerReads": int(metrics.get("answer_reads") or 0),
                    "answerScrolls": int(metrics.get("answer_scrolls") or 0),
                    "domProbeChecks": int(metrics.get("dom_probe_checks") or 0),
                    "domDoneSignals": int(metrics.get("dom_done_signals") or 0),
                    "overlayChecks": int(metrics.get("overlay_checks") or 0),
                    "overlayScans": int(metrics.get("overlay_scans") or 0),
                    "overlaySkips": int(metrics.get("overlay_scan_skips") or 0),
                    "snapshots": int(metrics.get("snapshots") or 0),
                    "finalAnswerChars": int(metrics.get("final_answer_chars") or 0),
                    "finalCompactChars": int(metrics.get("final_compact_chars") or 0),
                    "durationSeconds": float(metrics.get("duration_seconds") or 0.0),
                    "recordedAt": _local_iso_seconds(),
                }
                self._test_run_state.append_poll_diagnostic(run_id, item, limit=50)
                return
            if stage == "query_done":
                completed_queries = int(payload.get("completed_queries") or 0)
                total_queries = int(payload.get("total_queries") or initial_state["totalQueries"])
                succeeded = bool(payload.get("success"))
                self._set_test_run_state(run_id, {
                    "status": "running",
                    "currentQuery": min(completed_queries + 1, total_queries) if completed_queries < total_queries else completed_queries,
                    "completedQueries": completed_queries,
                    "totalQueries": total_queries,
                    "hitQueries": int(payload.get("hit_queries") or 0),
                    "currentKeyword": str(payload.get("keyword") or "").strip(),
                    "currentPlatform": str(payload.get("platform") or "").strip(),
                    "message": "本次命中目标" if succeeded else "本次未命中目标",
                    "errorMessage": str(payload.get("error_message") or "").strip(),
                })
                return
            if stage == "finished":
                success = bool(payload.get("success"))
                task_status = str(payload.get("task_status") or "").strip()
                task_failure_kind = str(payload.get("task_failure_kind") or "").strip()
                detail_message = resolve_report_display_message(payload)
                resolved_status = "success" if success else "failed"
                resolved_result = "success" if success else "failed"
                if task_failure_kind == "cancelled" or task_status == "pending":
                    resolved_status = "cancelled"
                    resolved_result = "cancelled"
                self._set_test_run_state(run_id, {
                    "status": resolved_status,
                    "currentQuery": int(payload.get("total_queries") or initial_state["totalQueries"]),
                    "completedQueries": int(payload.get("completed_queries") or 0),
                    "totalQueries": int(payload.get("total_queries") or initial_state["totalQueries"]),
                    "hitQueries": int(payload.get("hit_queries") or 0),
                    "message": detail_message,
                    "result": resolved_result,
                    "finishedAt": _local_iso_seconds(),
                })

        def _worker() -> None:
            session_manager = self._create_query_session_manager(
                [target_task],
                config,
                str(config.get("detection_mode", "browser") or "").strip(),
                source_label="异步手动测试",
            )

            try:
                result_items, report = run_task_group(
                    target_task,
                    default_notification,
                    config,
                    force_notify=True,
                    execution_source="manual_test",
                    return_report=True,
                    progress_callback=_on_progress,
                    stop_checker=cancel_event.is_set,
                    platform_session_manager=session_manager,
                )
                hit = sum(1 for r in result_items if r.get("rank", 99) != 99)
                task_success = is_report_success(report)
                task_status = str(resolve_report_status(report, fallback="") or "").strip()
                task_failure_kind = str(report.get("task_failure_kind") or "").strip()
                final_message = resolve_report_display_message(report)
                if task_failure_kind == "cancelled" or task_status == "pending":
                    failed_keyword, failed_platform, error_message = self._resolve_test_run_failure_context(run_id, report)
                    self._set_test_run_state(run_id, {
                        "status": "cancelled",
                        "completedQueries": len(result_items),
                        "currentQuery": len(result_items),
                        "totalQueries": max(initial_state["totalQueries"], len(result_items)),
                        "hitQueries": hit,
                        "currentKeyword": failed_keyword,
                        "currentPlatform": failed_platform,
                        "message": str(report.get("task_failure_message") or "测试已中断").strip() or "测试已中断",
                        "result": "cancelled",
                        "errorMessage": error_message,
                        "failureDetails": self._build_test_run_failure_details(report),
                        "finishedAt": _local_iso_seconds(),
                        "report": report,
                    })
                elif task_success:
                    self._clear_test_failure_notice(task_id)
                    self._set_test_run_state(run_id, {
                        "status": "success",
                        "completedQueries": len(result_items),
                        "currentQuery": len(result_items),
                        "totalQueries": max(initial_state["totalQueries"], len(result_items)),
                        "hitQueries": hit,
                        "message": final_message,
                        "result": "success",
                        "failureDetails": [],
                        "sendableSuccessCount": 0,
                        "actualScreenshotCount": 0,
                        "canForceSendSuccess": False,
                        "finishedAt": _local_iso_seconds(),
                        "report": report,
                    })
                else:
                    failed_keyword, failed_platform, error_message = self._resolve_test_run_failure_context(run_id, report)
                    task_day_status = get_task_day_status(target_task)
                    if str(task_day_status.get("brand_status") or "").strip() in {"success", "sent"}:
                        self._clear_test_failure_notice(task_id)
                    else:
                        self._set_test_failure_notice(task_id, final_message, run_id=run_id)
                    self._set_test_run_state(run_id, {
                        "status": "failed",
                        "completedQueries": len(result_items),
                        "currentQuery": len(result_items),
                        "totalQueries": max(initial_state["totalQueries"], len(result_items)),
                        "hitQueries": hit,
                        "currentKeyword": failed_keyword,
                        "currentPlatform": failed_platform,
                        "message": final_message,
                        "result": "failed",
                        "errorMessage": error_message,
                        "failureDetails": self._build_test_run_failure_details(report),
                        **self._build_test_run_force_send_snapshot(target_task),
                        "finishedAt": _local_iso_seconds(),
                        "report": report,
                    })
            except Exception as exc:
                print(f"[WebBackend] 异步测试运行异常: 任务={task_name}, {exc}")
                task_day_status = get_task_day_status(target_task)
                if str(task_day_status.get("brand_status") or "").strip() in {"success", "sent"}:
                    self._clear_test_failure_notice(task_id)
                else:
                    self._set_test_failure_notice(task_id, str(exc) or "测试失败", run_id=run_id)
                self._set_test_run_state(run_id, {
                    "status": "failed",
                    "message": "测试失败",
                    "result": "failed",
                    "errorMessage": str(exc),
                    "failureDetails": [],
                    **self._build_test_run_force_send_snapshot(target_task),
                    "finishedAt": _local_iso_seconds(),
                })
            finally:
                self._test_run_state.pop_cancel_event(run_id)
                if session_manager is not None:
                    session_manager.close_all(reason="异步手动测试结束")

        threading.Thread(target=_worker, name=f"test-run-{task_id}", daemon=True).start()
        return {"ok": True, "runId": run_id}

    def get_test_run_status(self, run_id: str) -> dict:
        with self._test_run_lock:
            self._prune_test_runs_locked()
            state = self._test_run_state.get(run_id)
            if not state:
                return {"ok": False, "message": "未找到测试任务"}
            snapshot = dict(state)
        if str(snapshot.get("status") or "").strip() == "failed":
            snapshot.update(self._build_test_run_force_send_snapshot(task_id=str(snapshot.get("taskId") or "").strip()))
        return {"ok": True, **snapshot}

    def start_batch_test(self, payload: dict) -> dict:
        """启动批量测试"""
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        from core.batch_test_runner import BatchTestRunner

        print(f"[WebBackend] 收到批量测试请求: {redact_secrets(payload)}")

        brand = str(payload.get("brand") or "").strip()
        keywords = payload.get("keywords") or []
        raw_inspect = payload.get("inspect", False)
        inspect = (
            str(raw_inspect).strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(raw_inspect, str)
            else bool(raw_inspect)
        )

        if not brand:
            return {"ok": False, "message": "品牌名称不能为空"}

        if not keywords:
            return {"ok": False, "message": "至少需要一个关键词"}

        # 生成 batch_id
        batch_id = uuid4().hex

        # 构建批量测试配置
        batch_config = {
            "batch_id": batch_id,
            "brand": brand,
            "inspect": inspect,
            "keywords": keywords,
            "status": "pending",
            "created_at": local_now().isoformat(),
            "progress": {
                "total_queries": 0,
                "completed_queries": 0,
                "current_keyword": "",
                "current_platform": "",
                "status_text": "准备中..."
            }
        }

        # 保存配置
        batch_file = _batch_test_file_path(batch_id)
        if batch_file is None:
            return {"ok": False, "message": "批量测试 ID 无效"}
        write_batch_json(batch_file, batch_config)

        print(f"[WebBackend] 批量测试配置已保存: {batch_file}")

        # 在后台线程启动测试
        def _worker():
            try:
                print(f"[WebBackend] 批量测试线程启动: {batch_id}")
                config = self.load_config()
                notifier = WeComNotifier(config)
                runner = BatchTestRunner(config, notifier)
                print(f"[WebBackend] 开始执行批量测试: {batch_id}")
                runner.run_batch_test(batch_config)
                print(f"[WebBackend] 批量测试完成: {batch_id}")
            except Exception as e:
                print(f"[WebBackend] 批量测试异常: {e}")
                import traceback
                traceback.print_exc()

        threading.Thread(target=_worker, name=f"batch-test-{batch_id}", daemon=True).start()
        print(f"[WebBackend] 批量测试线程已启动: batch-test-{batch_id}")

        return {"ok": True, "batch_id": batch_id}

    def get_batch_test_progress(self, batch_id: str) -> dict:
        """获取批量测试进度"""
        batch_file = _batch_test_file_path(batch_id)
        if batch_file is None:
            return {"ok": False, "message": "批量测试 ID 无效"}

        if not batch_file.exists():
            return {"ok": False, "message": "未找到批量测试任务"}

        try:
            batch_config = read_batch_json(batch_file)

            progress_data = batch_config.get("progress") or {}
            status = batch_config.get("status", "pending")

            return {
                "ok": True,
                "progress": {
                    "status": status,
                    "total_queries": progress_data.get("total_queries", 0),
                    "completed_queries": progress_data.get("completed_queries", 0),
                    "status_text": progress_data.get("status_text", "准备中..."),
                    "current_keyword": progress_data.get("current_keyword", ""),
                    "current_platform": progress_data.get("current_platform", "")
                }
            }
        except Exception as e:
            return {"ok": False, "message": f"读取进度失败: {e}"}

    def get_batch_test_report(self, batch_id: str) -> dict:
        """获取批量测试报告"""
        report_file = _batch_test_file_path(batch_id, "_report")
        if report_file is None:
            return {"ok": False, "message": "批量测试 ID 无效"}

        if not report_file.exists():
            return {"ok": False, "message": "报告尚未生成"}

        try:
            report = read_batch_json(report_file)

            return {"ok": True, "report": report}
        except Exception as e:
            return {"ok": False, "message": f"读取报告失败: {e}"}

    def cancel_batch_test(self, batch_id: str) -> dict:
        """取消批量测试"""
        batch_file = _batch_test_file_path(batch_id)
        if batch_file is None:
            return {"ok": False, "message": "批量测试 ID 无效"}

        if not batch_file.exists():
            return {"ok": False, "message": "未找到批量测试任务"}

        try:
            merge_batch_json(
                batch_file,
                {
                    "status": "cancelled",
                    "updated_at": local_now().isoformat(),
                },
            )

            return {"ok": True, "message": "已请求取消"}
        except Exception as e:
            return {"ok": False, "message": f"取消失败: {e}"}

    @staticmethod
    def _build_task_from_payload(payload: dict) -> dict:
        """从前端 payload 构建 config.yaml 格式的 task dict。"""
        task: dict[str, Any] = {}
        str_fields = ["name", "brand", "recognition_brands", "optimization_start_date", "optimization_end_date"]
        for f in str_fields:
            if f in payload:
                task[f] = str(payload[f]).strip()
        if "webhook_url" in payload:
            webhook_url = str(payload.get("webhook_url", "") or "").strip()
            if not _is_masked_secret(webhook_url):
                task["webhook_url"] = webhook_url
        bool_fields = ["enabled", "inspect", "recognition_enabled", "fixed_screenshot_enabled", "extract_references_enabled"]
        for f in bool_fields:
            if f in payload:
                task[f] = bool(payload[f])
        if "recognition_batch_size" in payload:
            try:
                task["recognition_batch_size"] = max(1, int(payload["recognition_batch_size"] or 1))
            except Exception:
                pass
        if "fixed_screenshot_count" in payload:
            try:
                task["fixed_screenshot_count"] = max(1, int(payload["fixed_screenshot_count"] or 1))
            except Exception:
                pass
        if "weekdays" in payload and isinstance(payload["weekdays"], list):
            task["weekdays"] = [int(d) for d in payload["weekdays"] if isinstance(d, (int, float))]
        if "industry_tags" in payload and isinstance(payload["industry_tags"], list):
            task["industry_tags"] = [str(t).strip() for t in payload["industry_tags"]]
        if "region_tags" in payload and isinstance(payload["region_tags"], list):
            task["region_tags"] = [str(t).strip() for t in payload["region_tags"]]
        if "keywords" in payload and isinstance(payload["keywords"], list):
            kws = []
            for kw_raw in payload["keywords"]:
                if not isinstance(kw_raw, dict):
                    continue
                kw = {
                    "keyword": str(kw_raw.get("keyword", "")).strip(),
                    "brand": str(kw_raw.get("brand", "")).strip(),
                    "platforms": [_normalize_platform_id(p) for p in kw_raw.get("platforms", [])],
                    "mode": str(kw_raw.get("mode", "browser")).strip(),
                }
                if kw["mode"] not in {"browser", "recognition"}:
                    kw["mode"] = "browser"
                if "deep_think" in kw_raw and isinstance(kw_raw["deep_think"], dict):
                    # 前端用显示名（"豆包"、"DeepSeek"等）作为 key，需要转换为平台 ID
                    kw["deep_think"] = {
                        _normalize_platform_id(k): v
                        for k, v in kw_raw["deep_think"].items()
                    }
                kws.append(kw)
            task["keywords"] = kws
        return task

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        raw = str(text or "").strip()
        if not raw:
            return None

        candidates = [raw]
        if "```" in raw:
            for block in raw.split("```"):
                block_text = block.strip()
                if not block_text:
                    continue
                if block_text.startswith("json"):
                    block_text = block_text[4:].strip()
                candidates.append(block_text)

        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidates.append(raw[start:end + 1])

        seen: set[str] = set()
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            try:
                parsed = json.loads(normalized)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _build_brand_draft_context(self, config: dict[str, Any]) -> str:
        platform_lines = []
        for pid, platform_cfg in (config.get("platforms", {}) or {}).items():
            if not platform_has_configured_access(config, pid):
                continue
            platform_lines.append(
                f"- {pid} / {_pid_to_display(pid)} / enabled={bool((platform_cfg or {}).get('enabled', False))} / model={str((platform_cfg or {}).get('api_model', '') or '').strip() or '未配置'}"
            )

        task_lines = []
        for task in (config.get("tasks", []) or [])[:12]:
            task_name = str(task.get("name") or derive_task_id(task)).strip()
            brand = str(task.get("brand") or task_name).strip()
            platforms = sorted({
                _normalize_platform_id(platform)
                for keyword in (task.get("keywords") or [])
                for platform in (keyword.get("platforms") or [])
                if str(platform or "").strip()
            })
            industry_tags = ", ".join(_normalize_string_list(task.get("industry_tags", []))) or "无"
            region_tags = ", ".join(_normalize_string_list(task.get("region_tags", []))) or "无"
            task_lines.append(
                f"- {task_name} | 品牌 {brand} | 平台 {', '.join(platforms) or '未配置'} | 行业 {industry_tags} | 地区 {region_tags}"
            )

        industry_tags = sorted({
            tag
            for task in (config.get("tasks", []) or [])
            for tag in _normalize_string_list(task.get("industry_tags", []))
        })
        region_tags = sorted({
            tag
            for task in (config.get("tasks", []) or [])
            for tag in _normalize_string_list(task.get("region_tags", []))
        })

        sections = [
            "当前可用平台：",
            "\n".join(platform_lines) if platform_lines else "- 暂无可用平台",
            f"现有行业标签：{', '.join(industry_tags) if industry_tags else '无'}",
            f"现有地区标签：{', '.join(region_tags) if region_tags else '无'}",
            "现有品牌任务示例：",
            "\n".join(task_lines) if task_lines else "- 暂无历史任务",
        ]
        return "\n".join(sections)

    def _normalize_brand_task_draft(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        configured_platforms = [
            _normalize_platform_id(pid)
            for pid, platform_cfg in (config.get("platforms", {}) or {}).items()
            if platform_has_configured_access(config, pid)
        ]
        enabled_platforms = [
            _normalize_platform_id(pid)
            for pid, platform_cfg in (config.get("platforms", {}) or {}).items()
            if platform_has_configured_access(config, pid) and bool((platform_cfg or {}).get("enabled", False))
        ]
        default_platforms = enabled_platforms or configured_platforms

        brand = str(payload.get("brand", "") or payload.get("name", "")).strip()
        name = str(payload.get("name", "") or brand).strip()

        platforms = _normalize_platform_list(payload.get("platforms", []))
        if not platforms:
            platforms = list(default_platforms)

        deep_platforms = [
            platform for platform in _normalize_platform_list(payload.get("deep_think_platforms", []))
            if platform in platforms
        ]

        weekdays = _normalize_weekdays(payload.get("weekdays", list(range(7))))
        if not weekdays:
            weekdays = list(range(7))

        keywords: list[dict[str, Any]] = []
        for raw_keyword in payload.get("keywords", []) or []:
            if not isinstance(raw_keyword, dict):
                continue
            keyword_text = str(raw_keyword.get("keyword", "") or "").strip()
            if not keyword_text:
                continue
            keyword_platforms = _normalize_platform_list(raw_keyword.get("platforms", [])) or list(platforms)
            keyword_deep = [
                platform for platform in _normalize_platform_list(raw_keyword.get("deep_think_platforms", []))
                if platform in keyword_platforms
            ]
            mode = str(raw_keyword.get("mode", "") or "browser").strip()
            if mode not in {"browser", "recognition"}:
                mode = "browser"
            keywords.append(
                {
                    "keyword": keyword_text,
                    "brand": str(raw_keyword.get("brand", "") or brand or name).strip(),
                    "platforms": keyword_platforms,
                    "mode": mode,
                    "deep_think_platforms": keyword_deep,
                }
            )

        if not keywords and brand:
            keywords.append(
                {
                    "keyword": brand,
                    "brand": brand,
                    "platforms": list(platforms),
                    "mode": "browser",
                    "deep_think_platforms": list(deep_platforms),
                }
            )

        return {
            "name": name,
            "brand": brand or name,
            "recognition_brands": _normalize_string_list(payload.get("recognition_brands", [])),
            "industry_tags": _normalize_string_list(payload.get("industry_tags", [])),
            "region_tags": _normalize_string_list(payload.get("region_tags", [])),
            "webhook_url": str(payload.get("webhook_url", "") or "").strip(),
            "optimization_start_date": str(payload.get("optimization_start_date", "") or "").strip(),
            "optimization_end_date": str(payload.get("optimization_end_date", "") or "").strip(),
            "weekdays": weekdays,
            "enabled": bool(payload.get("enabled", False)),
            "inspect": bool(payload.get("inspect", False)),
            "recognition_enabled": bool(payload.get("recognition_enabled", False)),
            "recognition_batch_size": max(1, _safe_int(payload.get("recognition_batch_size", 1), 1)),
            "extract_references_enabled": bool(payload.get("extract_references_enabled", False)),
            "fixed_screenshot_enabled": bool(payload.get("fixed_screenshot_enabled", False)),
            "fixed_screenshot_count": max(1, _safe_int(payload.get("fixed_screenshot_count", 3), 3)),
            "platforms": platforms,
            "deep_think_platforms": deep_platforms,
            "keywords": keywords,
            "notes": _normalize_string_list(payload.get("notes", [])),
            "missing_info": _normalize_string_list(payload.get("missing_info", [])),
        }

    def generate_brand_task_draft(self, payload: dict[str, Any]) -> dict:
        instruction = str(payload.get("instruction", "") or "").strip()
        if not instruction:
            return {"ok": False, "message": "instruction 不能为空"}

        config = self.load_config()
        ai_cfg = config.get("ai_assistant", {}) or {}
        platform = _normalize_platform_id(str(ai_cfg.get("platform", "") or "doubao").strip())
        model = str(ai_cfg.get("model", "") or "").strip() or (PLATFORM_API_CONFIG.get(platform) or {}).get("default_model", "")
        api_key = get_platform_api_key(config, platform)
        if platform_requires_api_key(platform) and not api_key:
            return {"ok": False, "message": f"平台 {platform} 未配置 API Key"}

        system_prompt = (
            "你是 Surfaced 的品牌任务编排助手。"
            "你的职责是把用户的一整段品牌监控需求，拆成可以直接落到系统里的单个品牌任务草稿。"
            "只返回 JSON，不要输出解释、标题或 Markdown 代码块。"
            "只按用户明确写出来的要求做字段拆解，不要额外脑补监控目标、风险点、建议方案或业务意图。"
            "用户没提到的字段，保持空值或默认值，不要为了凑完整而自行补全。"
            "平台、模式、时间、识别设置、截图设置都只在用户明确提及时填写；否则保持默认。"
            "可以复用当前系统里已经存在的平台 ID、行业标签、地区标签格式，但不要替用户做额外策略判断。"
            "不要虚构 webhook、日期或用户未给出的明确业务事实；不确定时留空。"
            "keywords 里只放用户需求中明确提到的品牌词、产品词、竞品词、主题词，不要擅自扩写。"
            "weekdays 使用 0-6 表示周一到周日。"
        )
        user_prompt = (
            "请根据下面的系统上下文和用户需求，输出一个品牌任务草稿 JSON。\n\n"
            "JSON schema:\n"
            "{\n"
            '  "name": "任务名称",\n'
            '  "brand": "品牌名",\n'
            '  "recognition_brands": ["别名1", "别名2"],\n'
            '  "industry_tags": ["行业"],\n'
            '  "region_tags": ["地区"],\n'
            '  "webhook_url": "",\n'
            '  "optimization_start_date": "",\n'
            '  "optimization_end_date": "",\n'
            '  "weekdays": [0, 1, 2, 3, 4, 5, 6],\n'
            '  "enabled": false,\n'
            '  "inspect": false,\n'
            '  "recognition_enabled": false,\n'
            '  "recognition_batch_size": 1,\n'
            '  "extract_references_enabled": false,\n'
            '  "fixed_screenshot_enabled": false,\n'
            '  "fixed_screenshot_count": 3,\n'
            '  "platforms": ["doubao", "deepseek"],\n'
            '  "deep_think_platforms": ["deepseek"],\n'
            '  "keywords": [\n'
            '    {\n'
            '      "keyword": "监控词",\n'
            '      "brand": "品牌名",\n'
            '      "platforms": ["doubao"],\n'
            '      "mode": "browser",\n'
            '      "deep_think_platforms": []\n'
            "    }\n"
            "  ],\n"
            '  "notes": ["你做出的关键假设"],\n'
            '  "missing_info": ["还需要用户补充的信息"]\n'
            "}\n\n"
            "规则补充：\n"
            "- mode 只能是 browser / recognition。\n"
            "- 如果用户没指定运行日，默认给全周。\n"
            "- 如果用户只明确提到品牌词，就只保留品牌词；不要擅自补充更多关键词。\n"
            "- platforms 和 deep_think_platforms 都使用平台 ID，不要用中文显示名。\n"
            "- recognition_brands、industry_tags、region_tags、notes、missing_info 都返回数组；如果没有就返回空数组。\n\n"
            f"系统上下文：\n{self._build_brand_draft_context(config)}\n\n"
            f"用户需求：\n{instruction}"
        )

        try:
            reply = send_platform_chat_messages(
                platform,
                api_key,
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        if reply is None:
            return {"ok": False, "message": get_platform_last_error(platform) or "AI 未返回可解析的品牌草稿"}

        parsed = self._extract_json_object(reply or "")
        if not parsed:
            return {"ok": False, "message": "AI 未返回可解析的品牌草稿", "raw_reply": reply or ""}

        return {
            "ok": True,
            "draft": self._normalize_brand_task_draft(parsed, config),
            "raw_reply": reply or "",
        }

    def _build_quick_todo_draft_context(self, config: dict[str, Any]) -> str:
        todos = self._normalize_and_store_todos(config, save=False)
        todo_lines = [
            f"- [{'x' if bool(item.get('done', False)) else ' '}] {str(item.get('text', '')).strip()}"
            for item in todos[:12]
            if str(item.get("text", "")).strip()
        ]
        task_lines = []
        for task in (config.get("tasks", []) or [])[:10]:
            task_name = str(task.get("name") or derive_task_id(task)).strip()
            brand = str(task.get("brand") or task_name).strip()
            task_lines.append(f"- {task_name} | 品牌 {brand}")
        return "\n".join([
            "当前快速待办：",
            "\n".join(todo_lines) if todo_lines else "- 暂无",
            "当前品牌任务：",
            "\n".join(task_lines) if task_lines else "- 暂无",
        ])

    def generate_quick_todos_draft(self, payload: dict[str, Any]) -> dict:
        instruction = str(payload.get("instruction", "") or "").strip()
        if not instruction:
            return {"ok": False, "message": "instruction 不能为空"}

        config = self.load_config()
        ai_cfg = config.get("ai_assistant", {}) or {}
        platform = _normalize_platform_id(str(ai_cfg.get("platform", "") or "doubao").strip())
        model = str(ai_cfg.get("model", "") or "").strip() or (PLATFORM_API_CONFIG.get(platform) or {}).get("default_model", "")
        api_key = get_platform_api_key(config, platform)
        if platform_requires_api_key(platform) and not api_key:
            return {"ok": False, "message": f"平台 {platform} 未配置 API Key"}

        system_prompt = (
            "你是 Surfaced 的快速待办拆解助手。"
            "你的职责是把用户的一段话拆成几条简洁、可执行、适合放进快速待办里的事项。"
            "只按用户明确写出的内容拆分，不要补充策略建议、额外动作或延伸任务。"
            "只返回 JSON，不要输出解释、标题或 Markdown 代码块。"
        )
        user_prompt = (
            "请根据下面的上下文和用户需求，把内容拆成待办数组 JSON。\n\n"
            "JSON schema:\n"
            "{\n"
            '  "todos": ["待办1", "待办2", "待办3"]\n'
            "}\n\n"
            "规则：\n"
            "- 每条待办都要短、明确、能直接执行。\n"
            "- 不要写成总结句、分析句或解释句。\n"
            "- 不要擅自新增用户没提到的工作。\n"
            "- 如果一句就够，只返回一条。\n"
            "- 如果用户表达的是一个复合任务，可以拆成 2 到 6 条。\n\n"
            f"系统上下文：\n{self._build_quick_todo_draft_context(config)}\n\n"
            f"用户需求：\n{instruction}"
        )

        try:
            reply = send_platform_chat_messages(
                platform,
                api_key,
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        if reply is None:
            return {"ok": False, "message": get_platform_last_error(platform) or "AI 未返回可解析的快速待办草稿"}

        parsed = self._extract_json_object(reply or "")
        if not parsed:
            return {"ok": False, "message": "AI 未返回可解析的快速待办草稿", "raw_reply": reply or ""}

        todos = _normalize_string_list(parsed.get("todos", []))
        if not todos:
            return {"ok": False, "message": "AI 未拆解出有效待办", "raw_reply": reply or ""}
        return {
            "ok": True,
            "todos": [{"text": text, "done": False} for text in todos],
            "raw_reply": reply or "",
        }

    # ── AI Chat ──────────────────────────────────────────────────

    def _prepare_chat_request(self, payload: dict) -> dict[str, Any]:
        platform = _normalize_platform_id(str(payload.get("platform", "") or "").strip())
        model = str(payload.get("model", "") or "").strip()
        messages = payload.get("messages", [])
        raw_deep_think = payload.get("deep_think")
        if not isinstance(messages, list) or not messages:
            return {"ok": False, "message": "messages 不能为空"}

        config = self.load_config()
        outgoing_messages = list(messages)

        # 如果没指定 platform 但指定了 model，根据 model 名反查所属平台
        if not platform and model:
            platforms_cfg = config.get("platforms", {}) or {}
            for pid, pcfg in PLATFORM_API_CONFIG.items():
                # 检查默认模型
                if pcfg.get("default_model", "") == model:
                    platform = pid
                    break
                # 检查用户配置的模型
                user_pcfg = _get_platform_config_entry(config, pid)
                if str(user_pcfg.get("api_model", "")).strip() == model:
                    platform = pid
                    break
                # 检查 model_options 列表
                opts = list(user_pcfg.get("model_options", []) or [])
                if model in opts:
                    platform = pid
                    break

        if not platform:
            # 默认使用 ai_assistant 配置的平台
            ai_cfg = config.get("ai_assistant", {}) or {}
            platform = str(ai_cfg.get("platform", "doubao") or "doubao").strip()
        if not model:
            ai_cfg = config.get("ai_assistant", {}) or {}
            model = str(ai_cfg.get("model", "") or "").strip()
        if not model:
            model = (PLATFORM_API_CONFIG.get(platform) or {}).get("default_model", "")

        if platform == "local_model":
            print("[WebBackend] skip assistant analysis context for local_model chat")
        elif _should_attach_assistant_analysis(outgoing_messages):
            try:
                analysis_payload = _build_monitoring_analysis_payload(
                    config,
                    articles=self._get_synced_articles(config),
                )
                analysis_message = {
                    "role": "system",
                    "content": _format_assistant_analysis_context(analysis_payload),
                }
                insert_at = 0
                for index, message in enumerate(outgoing_messages):
                    if str(message.get("role") or "").strip() == "system":
                        insert_at = index + 1
                        continue
                    break
                outgoing_messages.insert(insert_at, analysis_message)
                print(
                    f"[WebBackend] attached assistant analysis context for platform={platform or '(unknown)'} "
                    f"messages={len(outgoing_messages)}"
                )
            except Exception as exc:
                print(f"[WebBackend] 构建助手分析上下文失败: {exc}")

        if raw_deep_think is None:
            deep_think = platform == "local_model"
        else:
            deep_think = bool(raw_deep_think)

        api_key = get_platform_api_key(config, platform)
        if platform_requires_api_key(platform) and not api_key:
            return {"ok": False, "message": f"平台 {platform} 未配置 API Key"}

        return {
            "ok": True,
            "platform": platform,
            "model": model,
            "api_key": api_key,
            "messages": outgoing_messages,
            "deep_think": deep_think,
        }

    def chat(self, payload: dict) -> dict:
        """代理 AI 对话请求到各平台。"""
        prepared = self._prepare_chat_request(payload)
        if not prepared.get("ok"):
            return {"ok": False, "message": str(prepared.get("message") or "聊天请求不合法")}

        try:
            platform = str(prepared.get("platform") or "")
            reply = send_platform_chat_messages(
                platform,
                str(prepared.get("api_key") or ""),
                str(prepared.get("model") or ""),
                list(prepared.get("messages") or []),
                deep_think=bool(prepared.get("deep_think")),
            )
            if reply is None:
                return {"ok": False, "message": get_platform_last_error(platform) or f"平台 {platform} 接口没有返回内容"}
            return {"ok": True, "reply": reply or ""}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def chat_stream(self, payload: dict):
        prepared = self._prepare_chat_request(payload)
        if not prepared.get("ok"):
            yield {"type": "error", "message": str(prepared.get("message") or "聊天请求不合法")}
            return

        try:
            yield from send_platform_chat_messages_stream(
                str(prepared.get("platform") or ""),
                str(prepared.get("api_key") or ""),
                str(prepared.get("model") or ""),
                list(prepared.get("messages") or []),
                deep_think=bool(prepared.get("deep_think")),
            )
        except Exception as exc:
            yield {"type": "error", "message": str(exc)}

    # ── Search File Skills ───────────────────────────────────────

    def save_search_upload(self, file_name: str, data: bytes) -> dict:
        original_name = Path(str(file_name or "uploaded.xlsx")).name
        suffix = Path(original_name).suffix.lower()
        if suffix not in SEARCH_UPLOAD_EXTENSIONS:
            return {"ok": False, "message": "目前只支持上传 .xlsx/.xlsm 表格"}
        if not data:
            return {"ok": False, "message": "上传文件为空"}
        if len(data) > MAX_SEARCH_UPLOAD_BYTES:
            return {"ok": False, "message": "上传文件超过 25MB"}

        file_id = uuid4().hex
        upload_dir = resolve_app_path("exports/search_uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self._safe_upload_name(original_name)
        path = upload_dir / f"{file_id}_{safe_name}"
        path.write_bytes(data)

        item = {
            "id": file_id,
            "name": original_name,
            "path": str(path),
            "size": len(data),
            "uploaded_at": local_now().isoformat(timespec="seconds"),
        }
        with self._lock:
            self._prune_search_file_caches_locked()
            self._search_uploads[file_id] = item
        return {"ok": True, "file": item}

    def import_keywords_from_file(self, file_name: str, data: bytes) -> dict:
        original_name = Path(str(file_name or "keywords.xlsx")).name
        suffix = Path(original_name).suffix.lower()
        if suffix not in KEYWORD_IMPORT_EXTENSIONS:
            return {"ok": False, "message": "目前支持导入 .xlsx/.xlsm/.csv 表格和 .docx/.doc Word 文档"}
        if not data:
            return {"ok": False, "message": "上传文件为空"}
        if len(data) > MAX_SEARCH_UPLOAD_BYTES:
            return {"ok": False, "message": "上传文件超过 25MB"}

        try:
            keywords, details = _extract_keyword_import_items(original_name, data)
        except Exception as exc:
            return {"ok": False, "message": f"关键词导入失败：{exc}"}
        if not keywords:
            return {
                "ok": False,
                "message": "没有识别到可导入关键词，请确认文件里包含“关键词 / 搜索词 / 查询词”等列或列表内容",
                "file_name": original_name,
                "keywords": [],
                "details": details,
            }
        return {
            "ok": True,
            "message": f"已识别 {len(keywords)} 个关键词",
            "file_name": original_name,
            "keywords": keywords,
            "count": len(keywords),
            "details": details,
        }

    def run_search_brand_rank(self, payload: dict) -> dict:
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        file_id = str(payload.get("file_id", "") or "").strip()
        brand = str(payload.get("brand", "") or "").strip()
        if not file_id:
            return {"ok": False, "message": "请先上传表格文件"}
        if not brand:
            return {"ok": False, "message": "请在输入里写明品牌，例如：确认丸美品牌排名"}

        with self._lock:
            self._prune_search_file_caches_locked()
            upload = dict(self._search_uploads.get(file_id) or {})
        input_path = Path(str(upload.get("path") or ""))
        if not upload or not input_path.exists():
            return {"ok": False, "message": "上传文件已失效，请重新上传"}

        output_dir = resolve_app_path("exports/brand_ranker")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_name = f"{Path(str(upload.get('name') or input_path.name)).stem}_{brand}排名.xlsx"
        output_path = output_dir / self._safe_upload_name(output_name)

        try:
            summary = self._run_brand_rank_workbook(input_path, brand, output_path)
        except SystemExit as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": f"品牌排名处理失败：{exc}"}

        output_id = uuid4().hex
        with self._lock:
            self._prune_search_file_caches_locked()
            self._search_outputs[output_id] = {
                "id": output_id,
                "name": output_path.name,
                "path": str(output_path),
                "created_at": local_now().isoformat(timespec="seconds"),
            }

        return {
            "ok": True,
            "message": f"已完成「{brand}」品牌排名",
            "brand": brand,
            "input": str(input_path),
            "output": str(output_path),
            "file_name": output_path.name,
            "download_url": f"/api/search/download-file?id={output_id}",
            "summary": summary,
        }

    def get_search_output_file(self, output_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune_search_file_caches_locked()
            item = dict(self._search_outputs.get(output_id) or {})
        path = Path(str(item.get("path") or ""))
        if not item or not path.exists() or not path.is_file():
            return None
        item["path"] = str(path)
        item["name"] = str(item.get("name") or path.name)
        return item

    def _safe_upload_name(self, file_name: str) -> str:
        safe = "".join(
            ch if ch.isalnum() or ch in {"-", "_", ".", " ", "（", "）", "(", ")"} else "_"
            for ch in Path(file_name).name
        ).strip()
        return safe or f"upload_{uuid4().hex}.xlsx"

    def _run_brand_rank_workbook(self, input_path: Path, brand: str, output_path: Path) -> dict:
        try:
            from scripts import brand_ranker

            importlib.reload(brand_ranker)

            return brand_ranker.rank_workbook(
                input_path,
                brand=brand,
                output=output_path,
            )
        except SystemExit as exc:
            if "openpyxl" not in str(exc):
                raise
        except ImportError as exc:
            if "openpyxl" not in str(exc):
                raise

        return self._run_brand_ranker_subprocess(input_path, brand, output_path)

    def _run_brand_ranker_subprocess(self, input_path: Path, brand: str, output_path: Path) -> dict:
        script_path = resolve_app_path("scripts/brand_ranker.py")
        last_error = ""
        for python_path in self._brand_ranker_python_candidates():
            try:
                check = subprocess.run(
                    [python_path, "-c", "import openpyxl"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception as exc:
                last_error = str(exc)
                continue
            if check.returncode != 0:
                last_error = (check.stderr or check.stdout or "").strip()
                continue

            run = subprocess.run(
                [
                    python_path,
                    str(script_path),
                    str(input_path),
                    "--brand",
                    brand,
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if run.returncode != 0:
                last_error = (run.stderr or run.stdout or "").strip()
                continue
            try:
                return json.loads(run.stdout)
            except json.JSONDecodeError as exc:
                last_error = f"无法解析排名脚本输出：{exc}"

        raise RuntimeError(
            "当前运行环境缺少 openpyxl，且没有找到可用的备用 Python。"
            "请安装依赖：pip install openpyxl"
            + (f"（最后错误：{last_error}）" if last_error else "")
        )

    def _brand_ranker_python_candidates(self) -> list[str]:
        raw_candidates = [
            os.environ.get("BRAND_RANKER_PYTHON", ""),
            sys.executable,
            str(resolve_app_path(".venv/bin/python")),
            str(resolve_app_path("venv/bin/python")),
            str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"),
            "python3",
            "python",
        ]
        candidates: list[str] = []
        seen = set()
        for candidate in raw_candidates:
            value = str(candidate or "").strip()
            if not value or value in seen:
                continue
            if "/" in value and not Path(value).exists():
                continue
            seen.add(value)
            candidates.append(value)
        return candidates

    # ── Settings ─────────────────────────────────────────────────

    def get_settings(self) -> dict:
        """返回当前设置。"""
        return self.settings_service.get_settings()

    def diagnose_selector_heal(self, payload: dict) -> dict:
        """Run a read-only selector diagnosis."""
        return self.selector_heal_service.diagnose(payload)

    def apply_selector_heal(self, payload: dict) -> dict:
        """Apply a verified selector candidate."""
        return self.selector_heal_service.apply(payload)

    def diagnose_selector_pause_state(self, payload: dict) -> dict:
        """Probe the in-generation pause/stop state used by browser crawling."""
        return self.selector_heal_service.diagnose_pause_state(payload)

    def _selector_heal_runtime_safety(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        blockers: list[str] = []

        monitoring_running = self._is_monitoring_running()
        checks["monitoring_running"] = monitoring_running
        if monitoring_running:
            blockers.append("正式抓取任务正在运行，请先暂停抓取后再应用 selector。")

        scheduler_draining = False
        scheduler_running_tasks: dict[str, Any] = {}
        if self._scheduler is not None:
            try:
                scheduler_running_tasks = dict(self._scheduler.get_running_tasks() or {})
            except Exception:
                scheduler_running_tasks = {}
            scheduler_draining = bool(scheduler_running_tasks)
        checks["scheduler_draining"] = scheduler_draining
        checks["scheduler_running_tasks"] = scheduler_running_tasks
        if scheduler_draining:
            blockers.append("抓取任务正在暂停收尾，请等待当前浏览器动作完全结束后再应用 selector。")

        worker_running = bool(self._worker and self._worker.is_alive())
        checks["manual_run_running"] = worker_running
        if worker_running:
            blockers.append("手动执行任务正在运行，请等待结束后再应用 selector。")

        active_test_runs = self._test_run_state.active_run_ids()
        checks["active_test_runs"] = active_test_runs
        if active_test_runs:
            blockers.append("当前有测试任务正在运行，请等待结束后再应用 selector。")

        recognition_test_running = False
        manager = self._recognition_test_manager
        if manager is not None and hasattr(manager, "get_runtime_status"):
            try:
                recognition_test_running = bool((manager.get_runtime_status() or {}).get("running"))
            except Exception:
                recognition_test_running = True
        checks["recognition_test_running"] = recognition_test_running
        if recognition_test_running:
            blockers.append("识别测试正在运行，请先停止识别测试后再应用 selector。")

        active_batches = self._active_batch_test_ids()
        checks["active_batch_tests"] = active_batches
        if active_batches:
            blockers.append("批量测试正在运行，请等待结束或取消后再应用 selector。")

        blocking_reason = blockers[0] if blockers else ""
        return {
            "runtime_safe": not blockers,
            "blocking_reason": blocking_reason,
            "checks": checks,
        }

    @staticmethod
    def _active_batch_test_ids(*, stale_after_seconds: float = 6 * 60 * 60) -> list[str]:
        batch_dir = resolve_app_path("user_data/batch_tests")
        if not batch_dir.exists():
            return []
        active: list[str] = []
        now_ts = time.time()
        for path in batch_dir.glob("*.json"):
            if path.name.endswith("_report.json"):
                continue
            try:
                stat = path.stat()
                if now_ts - stat.st_mtime > stale_after_seconds:
                    continue
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle) or {}
            except Exception:
                continue
            status = str((payload or {}).get("status") or "").strip().lower()
            if status in {"pending", "running"}:
                batch_id = str((payload or {}).get("batch_id") or path.stem).strip()
                if batch_id:
                    active.append(batch_id)
        return active

    def _write_selector_heal_config(self, platform: str, field: str, selector: str) -> str:
        with self._lock:
            config = self.load_config()
            browser_cfg = config.get("browser_automation")
            if not isinstance(browser_cfg, dict):
                browser_cfg = {}
                config["browser_automation"] = browser_cfg
            platform_cfg = browser_cfg.get(platform)
            if not isinstance(platform_cfg, dict):
                platform_cfg = {}
                browser_cfg[platform] = platform_cfg
            previous_selector = str(platform_cfg.get(field) or "").strip()
            platform_cfg[field] = selector
            self.save_config(config)
            return previous_selector

    def save_settings(self, payload: dict) -> dict:
        """保存设置到 config.yaml。"""
        with self._lock:
            config = self.load_config()
            normalized_payload = copy.deepcopy(payload)

            scheduler_payload = normalized_payload.get("scheduler")
            if isinstance(scheduler_payload, dict) and "notification_webhook_url" in scheduler_payload:
                existing_scheduler = config.get("scheduler", {}) or {}
                scheduler_payload["notification_webhook_url"] = _preserve_masked_secret(
                    scheduler_payload.get("notification_webhook_url", ""),
                    existing_scheduler.get("notification_webhook_url", ""),
                )

            search_payload = normalized_payload.get("search")
            if isinstance(search_payload, dict) and "tavily_api_key" in search_payload:
                existing_search = config.get("search", {}) or {}
                search_payload["tavily_api_key"] = _preserve_masked_secret(
                    search_payload.get("tavily_api_key", ""),
                    existing_search.get("tavily_api_key", ""),
                )

            tavily_payload = normalized_payload.get("tavily")
            if isinstance(tavily_payload, dict) and "api_key" in tavily_payload:
                existing_search = config.get("search", {}) or {}
                tavily_payload["api_key"] = _preserve_masked_secret(
                    tavily_payload.get("api_key", ""),
                    existing_search.get("tavily_api_key", ""),
                )

            cloud_sync_payload = normalized_payload.get("cloud_sync")
            if isinstance(cloud_sync_payload, dict) and "api_token" in cloud_sync_payload:
                existing_cloud_sync = config.get("cloud_sync", {}) or {}
                cloud_sync_payload["api_token"] = _preserve_masked_secret(
                    cloud_sync_payload.get("api_token", ""),
                    existing_cloud_sync.get("api_token", ""),
                )

            image_generation_payload = normalized_payload.get("image_generation")
            if isinstance(image_generation_payload, dict):
                existing_image_generation = config.get("image_generation", {}) or {}
                normalized_payload["image_generation"] = _preserve_nested_api_keys(
                    image_generation_payload,
                    existing_image_generation,
                )

            app_update_payload = normalized_payload.get("app_update")
            if isinstance(app_update_payload, dict):
                app_update_payload["channel"] = normalize_update_channel(
                    app_update_payload.get("channel"),
                    default=get_app_update_settings(config).get("channel", "stable"),
                )
                app_update_payload["manifest_url"] = str(app_update_payload.get("manifest_url", "") or "").strip()
                app_update_payload["download_page_url"] = str(app_update_payload.get("download_page_url", "") or "").strip()
                app_update_payload["manifest_sha256"] = str(app_update_payload.get("manifest_sha256", "") or "").strip()
                app_update_payload["manifest_public_key"] = str(app_update_payload.get("manifest_public_key", "") or "").strip()
                app_update_payload["auto_check_enabled"] = bool(app_update_payload.get("auto_check_enabled", False))
                app_update_payload["require_signature"] = bool(app_update_payload.get("require_signature", False))
                app_update_payload["require_package_hash"] = bool(app_update_payload.get("require_package_hash", False))

            article_export_payload = normalized_payload.get("article_export")
            if isinstance(article_export_payload, dict):
                article_export_payload["show_keyword_category"] = bool(
                    article_export_payload.get("show_keyword_category", False)
                )
                if "show_selfmedia_account" in article_export_payload:
                    article_export_payload["show_selfmedia_account"] = bool(
                        article_export_payload.get("show_selfmedia_account", True)
                    )

            recognition_payload = normalized_payload.get("recognition")
            if isinstance(recognition_payload, dict):
                recognition_payload.pop("ai_fallback_enabled", None)
                recognition_payload.pop("platform", None)
                recognition_payload.pop("model", None)
                recognition_payload["safe_mode_ocr_enabled"] = True
                if "dom_render_mode" in recognition_payload:
                    recognition_payload["dom_render_mode"] = bool(
                        recognition_payload.get("dom_render_mode", False)
                    )
                if "floating_window_resident_enabled" in recognition_payload:
                    recognition_payload["floating_window_resident_enabled"] = bool(
                        recognition_payload.get("floating_window_resident_enabled", False)
                    )

            storage_payload = normalized_payload.get("storage")
            if isinstance(storage_payload, dict):
                storage_payload["history_read_backend"] = _normalize_history_read_backend_setting(
                    storage_payload.get("history_read_backend"),
                    default="auto",
                )
                storage_payload["history_write_backend"] = str(
                    storage_payload.get("history_write_backend") or "auto"
                ).strip().lower()
                storage_payload["history_shadow_writes_enabled"] = True

            context_snapshot_payload = normalized_payload.get("context_snapshots")
            if isinstance(context_snapshot_payload, dict):
                normalized_payload["context_snapshots"] = copy.deepcopy(DEFAULT_CONTEXT_SNAPSHOTS_CONFIG)

            allowed_sections = [
                "scheduler", "ai_assistant", "local_model", "recognition",
                "search", "profile", "cloud_sync",
                "default_notification",
                "context_snapshots",
                "query_execution",
                "account_crawling",
                "article_export",
                "app_update",
                "storage",
                "image_generation",
            ]
            query_execution_payload = normalized_payload.get("query_execution")
            if isinstance(query_execution_payload, dict):
                for mode in ("browser",):
                    mode_cfg = query_execution_payload.get(mode)
                    if not isinstance(mode_cfg, dict):
                        continue
                    mode_cfg["strategy"] = normalize_query_execution_strategy(
                        mode_cfg.get("strategy"),
                        default="session_pool",
                    )
                    mode_cfg["session_pool_dispatch"] = normalize_session_pool_dispatch(
                        mode_cfg.get("session_pool_dispatch"),
                        default="platform_batch",
                    )
            for section in allowed_sections:
                if section in normalized_payload and isinstance(normalized_payload[section], dict):
                    existing = config.get(section, {}) or {}
                    config[section] = _deep_merge_dict(existing, normalized_payload[section])
            if "context_snapshots" in normalized_payload:
                config["context_snapshots"] = get_context_snapshots_config(config)
            recognition_cfg = dict(config.get("recognition", {}) or {})
            recognition_cfg["safe_mode_ocr_enabled"] = True
            for obsolete_key in ("ai_fallback_enabled", "platform", "model"):
                recognition_cfg.pop(obsolete_key, None)
            config["recognition"] = recognition_cfg
            if "browser_automation" in normalized_payload and isinstance(normalized_payload["browser_automation"], dict):
                config["browser_automation"] = _merge_browser_automation_config(
                    config.get("browser_automation", {}) or {},
                    normalized_payload["browser_automation"],
                )
            for section in ("ai_assistant", "recognition"):
                section_cfg = config.get(section, {}) or {}
                if isinstance(section_cfg, dict) and section_cfg.get("platform"):
                    section_cfg["platform"] = _normalize_platform_id(str(section_cfg.get("platform", "") or "").strip())
                    config[section] = section_cfg
            if "tavily" in normalized_payload and isinstance(normalized_payload["tavily"], dict):
                search_cfg = dict(config.get("search", {}) or {})
                if "api_key" in normalized_payload["tavily"]:
                    search_cfg["tavily_api_key"] = str(normalized_payload["tavily"].get("api_key", "") or "").strip()
                config["search"] = search_cfg
            if "screenshot_template" in normalized_payload and isinstance(normalized_payload["screenshot_template"], dict):
                screenshot_cfg = dict(config.get("screenshot", {}) or {})
                screenshot_cfg["decoration"] = get_default_decoration_theme()
                config["screenshot"] = screenshot_cfg
            if "screenshot" in normalized_payload and isinstance(normalized_payload["screenshot"], dict):
                existing = config.get("screenshot", {}) or {}
                config["screenshot"] = _deep_merge_dict(existing, normalized_payload["screenshot"])
            if "image_generation" in normalized_payload and isinstance(normalized_payload["image_generation"], dict):
                config["image_generation"] = normalize_image_generation_config(config.get("image_generation", {}) or {})
            if "detection_mode" in normalized_payload and isinstance(normalized_payload["detection_mode"], str):
                config["detection_mode"] = normalized_payload["detection_mode"]
            _apply_guarded_history_storage_defaults(config, session=CloudSessionStore().load())
            self.save_config(config)
            get_local_model_manager().sync_config(config)
            configure_structured_history_storage(config)
            storage_cfg = config.get("storage") if isinstance(config.get("storage"), dict) else {}
            if (
                storage_cfg.get("history_read_backend") == "auto"
                or storage_cfg.get("history_write_backend") == "auto"
            ):
                maybe_schedule_structured_history_auto_rebuild("settings_save")
            self._sync_recognition_mode(config)
        self._refresh_monitoring_runtime(restart_scheduler=False)
        return {"ok": True}

    def check_app_update(self, payload: dict | None = None) -> dict:
        with self._lock:
            config = self.load_config()
        normalized_payload = payload or {}
        override_settings = normalized_payload.get("app_update")
        if isinstance(override_settings, dict):
            config = copy.deepcopy(config)
            existing = config.get("app_update", {}) or {}
            config["app_update"] = _deep_merge_dict(existing, override_settings)
        return build_update_status(config, include_check=True)

    def prepare_app_update(self, payload: dict | None = None) -> dict:
        with self._lock:
            config = self.load_config()
        normalized_payload = payload or {}
        override_settings = normalized_payload.get("app_update")
        if isinstance(override_settings, dict):
            config = copy.deepcopy(config)
            existing = config.get("app_update", {}) or {}
            config["app_update"] = _deep_merge_dict(existing, override_settings)

        status = build_update_status(config, include_check=True)
        if not status.get("ok"):
            return status
        download_url = str(status.get("download_url") or "").strip()
        package_sha256 = str(status.get("package_sha256") or "").strip()
        if not download_url:
            return {"ok": False, "message": "升级清单未提供当前平台下载地址", "status": status}
        if not package_sha256:
            return {"ok": False, "message": "升级清单未提供当前平台安装包 sha256，已拒绝下载", "status": status}
        try:
            prepared = prepare_update_package(
                download_url=download_url,
                expected_sha256=package_sha256,
            )
            plan_result = self.get_local_update_plan({"source_dir": prepared.get("source_dir", "")})
            if not plan_result.get("ok"):
                return {
                    "ok": False,
                    "message": str(plan_result.get("message") or "更新包已解压，但生成安装计划失败"),
                    "status": status,
                    "download": prepared,
                }
            return {
                "ok": True,
                "message": "更新包已下载、校验并解压，可以继续安装",
                "status": status,
                "download": prepared,
                "source_dir": prepared.get("source_dir", ""),
                "plan": plan_result.get("plan"),
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": f"准备更新包失败：{exc}",
                "status": status,
            }

    def _resolve_local_update_source(self, source_dir: str) -> tuple[Path | None, str]:
        source_text = str(source_dir or "").strip()
        if not source_text:
            return None, "请先提供已解压的新版本目录"
        source_path = Path(source_text).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            return None, "新版本目录不存在，或不是一个目录"
        target_path = get_runtime_app_dir().resolve()
        if source_path == target_path:
            return None, "新版本目录不能等于当前程序目录"
        if target_path in source_path.parents:
            return None, "新版本目录不能放在当前程序目录内部，否则更新时会被一并移动"
        if source_path in target_path.parents:
            return None, "新版本目录不能包含当前程序目录"
        required_markers = (
            "main.py",
            "web_backend.py",
            "Surfaced.app",
            "Surfaced.exe",
        )
        if not any((source_path / marker).exists() for marker in required_markers):
            return None, "新版本目录缺少可识别的程序入口，请确认选择的是已解压的应用根目录"
        return source_path, ""

    def get_local_update_plan(self, payload: dict | None = None) -> dict:
        normalized_payload = payload or {}
        source_path, error_message = self._resolve_local_update_source(
            str(normalized_payload.get("source_dir", "") or "")
        )
        if source_path is None:
            return {"ok": False, "message": error_message}
        try:
            plan = build_update_plan_payload(source_path)
            return {"ok": True, "plan": plan}
        except Exception as exc:
            return {"ok": False, "message": f"生成更新计划失败：{exc}"}

    def start_local_update(self, payload: dict | None = None) -> dict:
        normalized_payload = payload or {}
        source_path, error_message = self._resolve_local_update_source(
            str(normalized_payload.get("source_dir", "") or "")
        )
        if source_path is None:
            return {"ok": False, "message": error_message}
        cleanup_source = bool(normalized_payload.get("cleanup_source", False))
        restart_after_update = normalized_payload.get("restart_after_update", True) is not False
        exit_after_launch = normalized_payload.get("exit_after_launch", True) is not False
        try:
            process = launch_updater(
                source_dir=source_path,
                cleanup_source=cleanup_source,
                restart_after_update=restart_after_update,
            )
            exit_scheduled = False
            if exit_after_launch:
                exit_scheduled = self.request_app_exit()
            return {
                "ok": True,
                "message": (
                    "独立更新器已启动，程序即将退出并安装新版本"
                    if exit_scheduled
                    else "独立更新器已启动，请关闭当前程序后继续安装新版本"
                ),
                "pid": int(process.pid or 0),
                "exitScheduled": exit_scheduled,
            }
        except Exception as exc:
            return {"ok": False, "message": f"启动独立更新器失败：{exc}"}

    def refresh_context_snapshots(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        force = bool(payload.get("force", True))
        config, result = self._ensure_context_snapshots(force=force)
        weather = result.get("weather", {}) or {}
        calendar = result.get("calendar", {}) or {}
        ok = bool(weather.get("ok", True)) and bool(calendar.get("ok", True))
        return {
            "ok": ok,
            "message": "天气与节日提醒已刷新" if ok else "部分天气或节日提醒刷新失败",
            "weather": weather,
            "calendar": calendar,
            "weather_snapshot": config.get("weather_snapshot", {}),
            "calendar_snapshot": config.get("calendar_snapshot", {}),
            "context_snapshots": config.get("context_snapshots", {}),
        }

    # ── Todo ─────────────────────────────────────────────────────

    def get_todos(self) -> dict:
        """返回规范化后的 Todo 列表，并清理过期已完成项。"""
        return self.todos_service.get_todos()

    def sync_todos(self, payload: dict) -> dict:
        """同步 Todo 列表到 config.yaml。"""
        return self.todos_service.sync_todos(payload)

    # ── Articles ─────────────────────────────────────────────────

    def get_articles_filtered(
        self,
        media_type: str = "",
        limit: int = 50,
        task_name: str = "",
        include_export_keywords: bool = False,
    ) -> dict:
        """返回过滤后的文章列表。支持按 task_name 过滤。"""
        return self.article_service.get_articles_filtered(
            media_type,
            limit,
            task_name,
            include_export_keywords=include_export_keywords,
        )

    def export_task_articles_to_wecom(self, task_id: str, payload: dict | None = None) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        config = self.load_config()
        _, task, resolved_id = self._locate_task(config, task_id=str(task_id or "").strip())
        if not task or not resolved_id:
            return {"ok": False, "message": "未找到对应品牌任务"}

        webhook_url = str(task.get("webhook_url", "") or "").strip()
        if not webhook_url or "YOUR_KEY_HERE" in webhook_url:
            return {"ok": False, "message": "当前品牌未配置有效的企业微信 webhook"}

        task_name = str(task.get("name") or resolved_id).strip()
        brand_name = str(task.get("brand") or task_name).strip() or task_name
        filter_type = str(payload.get("type", "") or "").strip()
        start_date = str(payload.get("start", "") or "").strip()
        end_date = str(payload.get("end", "") or "").strip()

        articles = _apply_articles_account_context(self._get_synced_articles(config), config)
        articles = [article for article in articles if task_name in (article.get("matched_tasks") or [])]

        if filter_type:
            type_map = {"media": "authority", "self-media": "selfmedia"}
            target_type = type_map.get(filter_type, filter_type)
            articles = [article for article in articles if str(article.get("media_type", "") or "").strip() == target_type]

        if start_date:
            articles = [
                article for article in articles
                if str(article.get("ts", "") or "").strip()[:10] >= start_date
            ]
        if end_date:
            articles = [
                article for article in articles
                if str(article.get("ts", "") or "").strip()[:10] <= end_date
            ]

        if not articles:
            return {"ok": False, "message": "当前筛选条件下没有可发送的文章数据"}

        export_dir = resolve_app_path("exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = local_now().strftime("%Y%m%d_%H%M%S")
        file_name = f"{brand_name}-文章汇总-{timestamp}.xlsx"
        fd, temp_name = tempfile.mkstemp(prefix="article_export_", suffix=".xlsx", dir=str(export_dir))
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            article_export_cfg = config.get("article_export", {}) or {}
            _build_article_export_xlsx(
                brand_name=brand_name,
                start_date=start_date,
                end_date=end_date,
                articles=articles,
                output_path=temp_path,
                show_keyword_category=bool(article_export_cfg.get("show_keyword_category", False)),
                show_selfmedia_account=bool(article_export_cfg.get("show_selfmedia_account", True)),
                config=config,
                task_name=task_name,
            )
            from core.notifier import WeComNotifier

            notifier = WeComNotifier(webhook_url=webhook_url)
            ok = notifier.send_file_message(str(temp_path), file_name=file_name)
            if not ok:
                return {"ok": False, "message": notifier.last_error or "企业微信发送失败"}
            return {
                "ok": True,
                "message": f"已发送 {len(articles)} 篇文章汇总到企业微信",
                "fileName": file_name,
                "count": len(articles),
            }
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    def force_send_successful_task_results(self, task_id: str) -> dict:
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        config = self.load_config()
        _, task, resolved_id = self._locate_task(config, task_id=str(task_id or "").strip())
        if not task or not resolved_id:
            return {"ok": False, "message": "未找到对应品牌任务"}

        webhook_url = str(task.get("webhook_url", "") or "").strip()
        if not webhook_url or "YOUR_KEY_HERE" in webhook_url:
            return {"ok": False, "message": "当前品牌未配置有效的企业微信 webhook"}

        payload = _collect_today_successful_task_payload(task)
        screenshot_paths = list(payload.get("screenshotPaths") or [])
        actual_screenshot_count = int(payload.get("actualScreenshotCount") or 0)
        if actual_screenshot_count <= 0 or not screenshot_paths:
            return {"ok": False, "message": "当前任务今天没有可发送的成功截图"}

        brands = [str(item).strip() for item in (payload.get("brands") or []) if str(item).strip()]
        completed_keywords = [
            str(item).strip() for item in (payload.get("completedKeywords") or []) if str(item).strip()
        ]
        detected_platforms = [
            str(item).strip() for item in (payload.get("detectedPlatforms") or []) if str(item).strip()
        ]

        notifier = WeComNotifier(webhook_url=webhook_url)
        ok = notifier.send_detected_images(
            task_name=str(payload.get("taskName") or resolved_id).strip(),
            brands=brands,
            screenshot_paths=screenshot_paths,
            detected_platforms=detected_platforms,
            source="看板无视失败发送",
            completed_keywords=completed_keywords,
            total_screenshot_count=actual_screenshot_count,
        )
        if not ok:
            return {"ok": False, "message": notifier.last_error or "企业微信发送失败"}

        write_task_status(
            task,
            status="success",
            source=FORCE_SEND_SOURCE,
            message=f"看板无视失败后已发送 {actual_screenshot_count} 张成功截图",
            extra=build_task_state_extra(
                brands=list(brands),
                image_count=actual_screenshot_count,
                completed_keywords=list(completed_keywords),
                detected_platforms=list(detected_platforms),
                task_failure_kind="",
                notification_success=True,
                forced_ignore_failure=True,
            ),
        )
        try:
            enqueue_task_day_status({
                "task_id": task.get("cloud_task_id") or task.get("cloudTaskId"),
                "task_day": local_today().isoformat(),
                "status": "success",
                "source": FORCE_SEND_SOURCE,
                "message": f"看板无视失败后已发送 {actual_screenshot_count} 张成功截图",
                "brands": list(brands),
                "completed_keywords": list(completed_keywords),
                "detected_platforms": list(detected_platforms),
                "image_count": actual_screenshot_count,
                "actual_screenshot_count": actual_screenshot_count,
                "notification_success": True,
                "forced_ignore_failure": True,
                "updated_at": local_now().isoformat(timespec="seconds"),
            })
        except Exception as exc:
            print(f"[WebBackend] 强制发送成功状态云端同步入队失败，将保留本地成功状态: {exc}")
        self._invalidate_tasks_full_cache()
        task_name = str(payload.get("taskName") or task.get("name") or resolved_id).strip()
        for manager in (self._recognition_manager, self._recognition_test_manager):
            if manager is None:
                continue
            try:
                manager.suppress_task_for_today(task_name)
            except Exception:
                pass
        cycle_payload = update_cycle_report_with_forced_success(
            task_id=resolved_id,
            task_name=task_name,
            brands=list(brands),
            completed_keywords=list(completed_keywords),
            detected_platforms=list(detected_platforms),
            image_count=actual_screenshot_count,
        )
        if cycle_payload and self._scheduler_reporter is not None:
            try:
                self._scheduler_reporter._maybe_send_all_success_notification(cycle_payload)
            except Exception:
                pass
        success_message = f"已发送 {actual_screenshot_count} 张成功截图，并将任务改判为成功"
        self._clear_test_failure_notice(resolved_id)
        self._test_run_state.update_where(
            lambda state: (
                str(state.get("taskId") or "").strip() == resolved_id
                and str(state.get("status") or "").strip() == "failed"
            ),
            {
                "status": "success",
                "message": success_message,
                "result": "success",
                "errorMessage": "",
                "failureDetails": [],
                "sendableSuccessCount": actual_screenshot_count,
                "actualScreenshotCount": actual_screenshot_count,
                "canForceSendSuccess": False,
            },
        )
        return {
            "ok": True,
            "message": success_message,
            "actualScreenshotCount": actual_screenshot_count,
        }

    def import_article(self, payload: dict) -> dict:
        """通过 URL 导入文章。"""
        return self.article_service.import_article(payload)

    def import_articles_from_file(self, file_name: str, data: bytes) -> dict:
        """通过上传表格批量导入文章。"""
        return self.article_service.import_articles_from_file(file_name, data)

    def get_pending_article_imports(self) -> dict:
        """返回仍可确认/撤销的表格导入批次。"""
        return self.article_service.get_pending_article_imports()

    def confirm_article_import(self, import_id: str) -> dict:
        """确认一个待确认的表格导入批次。"""
        return self.article_service.confirm_article_import(import_id)

    def undo_article_import(self, import_id: str) -> dict:
        """撤销一个待确认的表格导入批次。"""
        return self.article_service.undo_article_import(import_id)

    def delete_article(self, article_id: str, task_name: str = "") -> dict:
        """删除一条文章记录，或仅从指定品牌文章汇总中移除。"""
        return self.article_service.delete_article(article_id, task_name)

    def update_article_media_type(self, article_id: str, media_type: str) -> dict:
        """更新文章媒体类型，并同步记忆同主域名站点。"""
        return self.article_service.update_article_media_type(article_id, media_type)

    def update_article(self, article_id: str, payload: dict) -> dict:
        """更新文章基础信息。"""
        return self.article_service.update_article(article_id, payload)

    # ── OCR Recognition ──────────────────────────────────────────

    def get_recognition_status(self, *, compact: bool = False, passive: bool = False) -> dict:
        """返回 OCR 识别状态，包含关键词引导信息。"""
        try:
            config = self.load_config()
            if self._recognition_test_manager is None and not passive:
                self._sync_recognition_mode(config)
            mgr, scope = self._get_active_recognition_manager(init_if_missing=not passive)
            if mgr:
                status = mgr.get_runtime_status(include_overview=not compact)
                status["keyword_guide"] = mgr.get_keyword_guide_state()
                if scope == "test":
                    status["scope"] = "test"
                    status["test_task_id"] = str((self._recognition_test_session or {}).get("task_id") or "").strip()
                    status["test_task_name"] = str((self._recognition_test_session or {}).get("task_name") or "").strip()
                return {"ok": True, "status": status}
            return {"ok": True, "status": {"running": False, "tasks": [], "keyword_guide": {"items": [], "index": 0}}}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def recognition_action(self, payload: dict) -> dict:
        """执行 OCR 识别操作。"""
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        action = str(payload.get("action", "")).strip()
        if not action:
            return {"ok": False, "message": "缺少 action 参数"}
        try:
            if action == "close_platform_browser":
                self._close_recognition_shared_browser()
                return {"ok": True, "message": "已关闭识别模式共享浏览器"}
            if action == "minimize_platform_browser":
                minimized = self._minimize_recognition_shared_browser()
                if minimized:
                    self._recognition_browser_minimized = True
                return {"ok": True, "message": "已最小化浏览器" if minimized else "未找到可最小化的浏览器窗口"}
            if action == "restore_platform_browser":
                restored = self._restore_recognition_shared_browser()
                if restored:
                    self._recognition_browser_minimized = False
                return {"ok": True, "message": "已恢复浏览器" if restored else "未找到可恢复的浏览器窗口"}
            if action == "toggle_platform_browser":
                toggled_to, changed = self._toggle_recognition_shared_browser()
                if toggled_to == "restore":
                    return {"ok": True, "message": "已恢复浏览器" if changed else "未找到可恢复的浏览器窗口"}
                return {"ok": True, "message": "已最小化浏览器" if changed else "未找到可最小化的浏览器窗口"}
            if action == "disable_recognition":
                self._close_recognition_shared_browser()
                mgr, scope = self._get_active_recognition_manager()
                if mgr:
                    if scope == "test":
                        self._stop_recognition_test_session(restore_previous=True)
                    else:
                        mgr.stop()
                return {"ok": True, "message": "识别模式已停用"}
            if action == "open_platform":
                mgr, _scope = self._get_active_recognition_manager(init_if_missing=False)
                return self._open_recognition_platform(mgr, payload)
            if action == "screenshot":
                return self._trigger_recognition_screenshot()

            mgr, scope = self._get_active_recognition_manager()
            if not mgr:
                return {"ok": False, "message": "识别管理器未初始化"}
            if action == "suppress_current_task_today":
                state = mgr.get_keyword_guide_state()
                items = list((state or {}).get("items") or [])
                index = int((state or {}).get("index") or 0)
                current = items[index] if 0 <= index < len(items) else {}
                task_name = str(payload.get("taskName") or current.get("task_name") or "").strip()
                if not task_name:
                    return {"ok": False, "message": "当前没有可停用的识别任务"}
                mgr.suppress_task_for_today(task_name, status_text=f"{task_name} 今日已停用")
                self._close_recognition_shared_browser()
                return {"ok": True, "message": f"{task_name} 今日已停用"}
            if scope == "test":
                if action == "start":
                    if not mgr.get_runtime_status().get("running", False):
                        mgr.start()
                    return {"ok": True, "message": "识别模式测试已启动"}
                if action == "stop":
                    self._stop_recognition_test_session(restore_previous=True)
                    return {"ok": True, "message": "识别模式测试已关闭"}
                if action in {"complete", "skip", "next", "prev"}:
                    mgr.handle_keyword_guide_action(action)
                    return {"ok": True}
                if action == "open_platform":
                    return self._open_recognition_platform(mgr, payload)
                if action == "screenshot":
                    return self._trigger_recognition_screenshot()
                return {"ok": False, "message": f"不支持的操作 {action}"}
            if action == "start":
                if mgr.has_recognition_tasks():
                    mgr.start()
                return {"ok": True}
            if action == "stop":
                mgr.stop()
                return {"ok": True}
            if action in {"complete", "skip", "next", "prev"}:
                mgr.handle_keyword_guide_action(action)
            elif action == "open_platform":
                return self._open_recognition_platform(mgr, payload)
            elif action == "screenshot":
                return self._trigger_recognition_screenshot()
            else:
                return {"ok": False, "message": f"不支持的操作 {action}"}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    # ── Assistant Tools ──────────────────────────────────────────

    def get_assistant_tools(self) -> dict:
        return {"ok": True, "tools": ASSISTANT_TOOLS}

    def _locate_task(
        self,
        config: dict[str, Any],
        *,
        task_id: str = "",
        task_name: str = "",
    ) -> tuple[int | None, dict[str, Any] | None, str]:
        tasks = config.get("tasks", []) or []
        for index, task in enumerate(tasks):
            current_id = str(task.get("task_id") or derive_task_id(task)).strip()
            current_name = str(task.get("name") or current_id).strip()
            if task_id and current_id == str(task_id).strip():
                return index, task, current_id
            if task_name and current_name == str(task_name).strip():
                return index, task, current_id
        return None, None, ""

    def _resolve_task_for_update(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int, dict[str, Any], str] | None:
        config = self.load_config()
        index, task, resolved_id = self._locate_task(
            config,
            task_id=str(payload.get("task_id", "") or "").strip(),
            task_name=str(payload.get("task_name", "") or "").strip(),
        )
        if index is None or task is None:
            return None
        tasks = list(config.get("tasks", []) or [])
        return config, tasks, index, dict(task), resolved_id

    def _save_task_entry(
        self,
        config: dict[str, Any],
        tasks: list[dict[str, Any]],
        index: int,
        task: dict[str, Any],
        *,
        message: str,
        record_optimization: bool = False,
    ) -> dict:
        if isinstance(task.get("keywords"), list):
            task["platforms"] = sorted({
                _normalize_platform_id(platform)
                for keyword in (task.get("keywords") or [])
                for platform in (keyword.get("platforms") or [])
                if str(platform or "").strip()
            })
        task["task_id"] = str(task.get("task_id") or derive_task_id(task)).strip()
        tasks[index] = task
        config["tasks"] = tasks
        self.save_config(config)
        self._invalidate_tasks_full_cache()
        self._invalidate_article_cache()
        if record_optimization:
            start = str(task.get("optimization_start_date", "") or "").strip()
            end = str(task.get("optimization_end_date", "") or "").strip()
            if start and end:
                save_optimization_period(str(task.get("name") or task["task_id"]).strip(), start, end)
        return {"ok": True, "task_id": task["task_id"], "message": message}

    @staticmethod
    def _normalize_keyword_input(keyword_payload: dict[str, Any], fallback_task: dict[str, Any]) -> dict[str, Any] | None:
        keyword_text = str(keyword_payload.get("keyword", "") or "").strip()
        if not keyword_text:
            return None
        platforms = _normalize_platform_list(keyword_payload.get("platforms", []))
        deep_platforms = _normalize_platform_list(keyword_payload.get("deep_think_platforms", []))
        item: dict[str, Any] = {
            "keyword": keyword_text,
            "brand": str(keyword_payload.get("brand", "") or fallback_task.get("brand", "") or fallback_task.get("name", "")).strip(),
            "platforms": platforms,
            "mode": str(keyword_payload.get("mode", "") or fallback_task.get("mode", "browser") or "browser").strip(),
        }
        if deep_platforms:
            item["deep_think"] = {platform: True for platform in deep_platforms if platform in platforms}
        return item

    @staticmethod
    def _keyword_identity(keyword_payload: dict[str, Any]) -> tuple[str, str]:
        return (
            str(keyword_payload.get("keyword", "") or "").strip().lower(),
            str(keyword_payload.get("brand", "") or "").strip().lower(),
        )

    @classmethod
    def _find_duplicate_keyword_index(
        cls,
        keywords: list[dict[str, Any]],
        candidate: dict[str, Any],
        skip_index: int | None = None,
    ) -> int:
        candidate_identity = cls._keyword_identity(candidate)
        if not candidate_identity[0]:
            return -1
        for index, keyword in enumerate(keywords):
            if skip_index is not None and index == skip_index:
                continue
            if cls._keyword_identity(keyword) == candidate_identity:
                return index
        return -1

    @staticmethod
    def _patch_keyword(existing: dict[str, Any], payload: dict[str, Any], fallback_task: dict[str, Any]) -> dict[str, Any]:
        keyword = dict(existing)
        if "keyword" in payload:
            keyword["keyword"] = str(payload.get("keyword", "") or "").strip()
        if "brand" in payload:
            keyword["brand"] = str(payload.get("brand", "") or "").strip()
        if "mode" in payload:
            keyword["mode"] = str(payload.get("mode", "") or "").strip() or str(fallback_task.get("mode", "browser") or "browser")
        if "platforms" in payload:
            keyword["platforms"] = _normalize_platform_list(payload.get("platforms", []))
        current_platforms = _normalize_platform_list(keyword.get("platforms", []))
        if "deep_think_platforms" in payload:
            deep_platforms = _normalize_platform_list(payload.get("deep_think_platforms", []))
            keyword["deep_think"] = {platform: True for platform in deep_platforms if platform in current_platforms}
        elif "deep_think" in keyword:
            keyword["deep_think"] = {
                _normalize_platform_id(platform): bool(enabled)
                for platform, enabled in (keyword.get("deep_think", {}) or {}).items()
                if bool(enabled) and _normalize_platform_id(platform) in current_platforms
            }
        keyword["brand"] = str(keyword.get("brand", "") or fallback_task.get("brand", "") or fallback_task.get("name", "")).strip()
        keyword["mode"] = str(keyword.get("mode", "") or fallback_task.get("mode", "browser") or "browser").strip()
        keyword["platforms"] = current_platforms
        if not keyword.get("deep_think"):
            keyword.pop("deep_think", None)
        return keyword

    def set_task_basic_info(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            changed = False
            old_brand = str(task.get("brand", "") or task.get("name", "")).strip()
            for field in ("name", "brand", "webhook_url", "recognition_brands"):
                if field in payload:
                    if field == "webhook_url":
                        task[field] = _preserve_masked_secret(
                            payload.get(field, ""),
                            task.get(field, ""),
                        )
                    else:
                        task[field] = str(payload.get(field, "") or "").strip()
                    changed = True
            new_brand = str(task.get("brand", "") or task.get("name", "")).strip()
            if changed and new_brand:
                updated_keywords = []
                for keyword in task.get("keywords", []) or []:
                    item = dict(keyword)
                    current_brand = str(item.get("brand", "") or "").strip()
                    if "brand" in payload or ("name" in payload and current_brand in {"", old_brand}):
                        item["brand"] = new_brand
                    updated_keywords.append(item)
                task["keywords"] = updated_keywords
            if not changed:
                return {"ok": False, "message": "未提供可更新的基础字段"}
            return self._save_task_entry(config, tasks, index, task, message=f"已更新任务 {task.get('name') or task_id} 的基础信息")

    def set_task_tags(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            changed = False
            if "industry_tags" in payload:
                task["industry_tags"] = _normalize_string_list(payload.get("industry_tags", []))
                changed = True
            if "region_tags" in payload:
                task["region_tags"] = _normalize_string_list(payload.get("region_tags", []))
                changed = True
            if not changed:
                return {"ok": False, "message": "未提供标签字段"}
            return self._save_task_entry(config, tasks, index, task, message=f"已更新任务 {task.get('name') or task_id} 的标签")

    def set_task_schedule(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            task["weekdays"] = _normalize_weekdays(payload.get("weekdays", []))
            return self._save_task_entry(config, tasks, index, task, message=f"已更新任务 {task.get('name') or task_id} 的执行日期")

    def set_task_runtime_options(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            changed = False
            for field in ("enabled", "inspect", "recognition_enabled", "fixed_screenshot_enabled"):
                if field in payload:
                    task[field] = bool(payload[field])
                    changed = True
            if "extract_references_enabled" in payload:
                task["extract_references_enabled"] = bool(payload["extract_references_enabled"])
                changed = True
            if "recognition_batch_size" in payload:
                try:
                    task["recognition_batch_size"] = max(1, int(payload.get("recognition_batch_size", 1) or 1))
                    changed = True
                except Exception:
                    return {"ok": False, "message": "recognition_batch_size 必须是整数"}
            if "fixed_screenshot_count" in payload:
                try:
                    task["fixed_screenshot_count"] = max(1, int(payload.get("fixed_screenshot_count", 1) or 1))
                    changed = True
                except Exception:
                    return {"ok": False, "message": "fixed_screenshot_count 必须是整数"}
            if not changed:
                return {"ok": False, "message": "未提供运行时字段"}
            return self._save_task_entry(config, tasks, index, task, message=f"已更新任务 {task.get('name') or task_id} 的运行配置")

    def set_task_optimization_period(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            changed = False
            for field in ("optimization_start_date", "optimization_end_date"):
                if field in payload:
                    task[field] = str(payload.get(field, "") or "").strip()
                    changed = True
            if not changed:
                return {"ok": False, "message": "未提供优化周期字段"}
            return self._save_task_entry(
                config,
                tasks,
                index,
                task,
                message=f"已更新任务 {task.get('name') or task_id} 的优化周期",
                record_optimization=True,
            )

    def set_task_platforms(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            platforms = _normalize_platform_list(payload.get("platforms", []))
            deep_platforms = _normalize_platform_list(payload.get("deep_think_platforms", []))
            keywords = []
            for keyword in task.get("keywords", []) or []:
                item = dict(keyword)
                item["platforms"] = platforms
                if deep_platforms:
                    item["deep_think"] = {platform: True for platform in deep_platforms if platform in platforms}
                else:
                    item.pop("deep_think", None)
                keywords.append(item)
            task["keywords"] = keywords
            task["platforms"] = platforms
            return self._save_task_entry(config, tasks, index, task, message=f"已更新任务 {task.get('name') or task_id} 的平台配置")

    def replace_task_keywords(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            raw_keywords = payload.get("keywords", [])
            if not isinstance(raw_keywords, list):
                return {"ok": False, "message": "keywords 必须是数组"}
            keywords = []
            for raw_keyword in raw_keywords:
                if not isinstance(raw_keyword, dict):
                    continue
                item = self._normalize_keyword_input(raw_keyword, task)
                if item:
                    if self._find_duplicate_keyword_index(keywords, item) >= 0:
                        continue
                    keywords.append(item)
            if raw_keywords and not keywords:
                return {"ok": False, "message": "未解析到有效关键词"}
            task["keywords"] = keywords
            task["platforms"] = sorted({platform for keyword in keywords for platform in keyword.get("platforms", [])})
            return self._save_task_entry(config, tasks, index, task, message=f"已替换任务 {task.get('name') or task_id} 的关键词")

    def add_task_keyword(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            item = self._normalize_keyword_input(payload, task)
            if not item:
                return {"ok": False, "message": "keyword 不能为空"}
            keywords = list(task.get("keywords", []) or [])
            duplicate_index = self._find_duplicate_keyword_index(keywords, item)
            if duplicate_index >= 0:
                return {
                    "ok": True,
                    "task_id": task_id,
                    "message": f"任务 {task.get('name') or task_id} 已存在关键词 {item['keyword']}，未重复添加",
                    "duplicate": True,
                    "keyword_index": duplicate_index,
                }
            keywords.append(item)
            task["keywords"] = keywords
            return self._save_task_entry(config, tasks, index, task, message=f"已为任务 {task.get('name') or task_id} 添加关键词 {item['keyword']}")

    def update_task_keyword(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            try:
                keyword_index = int(payload.get("keyword_index", -1))
            except Exception:
                keyword_index = -1
            keywords = list(task.get("keywords", []) or [])
            if keyword_index < 0 or keyword_index >= len(keywords):
                return {"ok": False, "message": "keyword_index 超出范围"}
            updated = self._patch_keyword(keywords[keyword_index], payload, task)
            if not str(updated.get("keyword", "") or "").strip():
                return {"ok": False, "message": "keyword 不能为空"}
            duplicate_index = self._find_duplicate_keyword_index(keywords, updated, skip_index=keyword_index)
            if duplicate_index >= 0:
                return {
                    "ok": False,
                    "message": f"关键词 {updated.get('keyword', '')} 已存在，无法修改为重复项",
                    "duplicate": True,
                    "keyword_index": duplicate_index,
                }
            keywords[keyword_index] = updated
            task["keywords"] = keywords
            return self._save_task_entry(config, tasks, index, task, message=f"已更新任务 {task.get('name') or task_id} 的关键词")

    def remove_task_keyword(self, payload: dict[str, Any]) -> dict:
        with self._lock:
            resolved = self._resolve_task_for_update(payload)
            if not resolved:
                return {"ok": False, "message": "未找到要更新的任务"}
            config, tasks, index, task, task_id = resolved
            try:
                keyword_index = int(payload.get("keyword_index", -1))
            except Exception:
                keyword_index = -1
            keywords = list(task.get("keywords", []) or [])
            if keyword_index < 0 or keyword_index >= len(keywords):
                return {"ok": False, "message": "keyword_index 超出范围"}
            removed = keywords.pop(keyword_index)
            task["keywords"] = keywords
            return self._save_task_entry(
                config,
                tasks,
                index,
                task,
                message=f"已从任务 {task.get('name') or task_id} 删除关键词 {removed.get('keyword', '')}",
            )

    def _update_single_platform_config(self, platform: str, patch: dict[str, Any], message: str) -> dict:
        real_platform = _normalize_platform_id(platform)
        result = self.save_platform_config({"platforms": {real_platform: patch}})
        if result.get("ok"):
            result["message"] = message
        return result

    def set_platform_api_key(self, payload: dict[str, Any]) -> dict:
        platform = str(payload.get("platform", "") or "").strip()
        if not platform:
            return {"ok": False, "message": "缺少 platform"}
        return self._update_single_platform_config(
            platform,
            {"api_key": str(payload.get("api_key", "") or "").strip()},
            f"已更新平台 {_pid_to_display(_normalize_platform_id(platform))} 的 API Key",
        )

    def set_platform_primary_model(self, payload: dict[str, Any]) -> dict:
        platform = str(payload.get("platform", "") or "").strip()
        model = str(payload.get("api_model", "") or "").strip()
        if not platform or not model:
            return {"ok": False, "message": "缺少 platform 或 api_model"}
        return self._update_single_platform_config(
            platform,
            {"api_model": model},
            f"已更新平台 {_pid_to_display(_normalize_platform_id(platform))} 的主模型",
        )

    def add_platform_model_option(self, payload: dict[str, Any]) -> dict:
        platform = _normalize_platform_id(str(payload.get("platform", "") or "").strip())
        model = str(payload.get("model", "") or "").strip()
        if not platform or not model:
            return {"ok": False, "message": "缺少 platform 或 model"}
        current_cfg = _get_platform_config_entry(self.load_config(), platform)
        options = _normalize_model_options(current_cfg.get("model_options", []))
        if model not in options:
            options.append(model)
        patch: dict[str, Any] = {"model_options": options}
        if bool(payload.get("make_primary", False)):
            patch["api_model"] = model
        return self._update_single_platform_config(platform, patch, f"已给平台 {_pid_to_display(platform)} 添加模型 {model}")

    def remove_platform_model_option(self, payload: dict[str, Any]) -> dict:
        platform = _normalize_platform_id(str(payload.get("platform", "") or "").strip())
        model = str(payload.get("model", "") or "").strip()
        if not platform or not model:
            return {"ok": False, "message": "缺少 platform 或 model"}
        current_cfg = _get_platform_config_entry(self.load_config(), platform)
        options = [item for item in _normalize_model_options(current_cfg.get("model_options", [])) if item != model]
        patch: dict[str, Any] = {"model_options": options}
        if str(current_cfg.get("api_model", "") or "").strip() == model:
            patch["api_model"] = options[0] if options else ""
        return self._update_single_platform_config(platform, patch, f"已从平台 {_pid_to_display(platform)} 移除模型 {model}")

    def replace_platform_model_options(self, payload: dict[str, Any]) -> dict:
        platform = _normalize_platform_id(str(payload.get("platform", "") or "").strip())
        options = _normalize_model_options(payload.get("model_options", []))
        if not platform:
            return {"ok": False, "message": "缺少 platform"}
        patch: dict[str, Any] = {"model_options": options}
        if "api_model" in payload:
            patch["api_model"] = str(payload.get("api_model", "") or "").strip()
        elif options:
            patch["api_model"] = options[0]
        return self._update_single_platform_config(platform, patch, f"已更新平台 {_pid_to_display(platform)} 的模型列表")

    def test_platform_connection(self, payload: dict[str, Any]) -> dict:
        platform = str(payload.get("platform", "") or "").strip()
        if not platform:
            return {"ok": False, "message": "缺少 platform"}
        result = self.test_platform(
            platform,
            {
                "api_key": str(payload.get("api_key", "") or "").strip(),
                "model": str(payload.get("model", "") or "").strip(),
            },
        )
        if result.get("ok"):
            result["message"] = f"{_pid_to_display(_normalize_platform_id(platform))} 连接成功"
        return result

    def set_detection_mode(self, payload: dict[str, Any]) -> dict:
        mode = str(payload.get("mode", "") or "").strip()
        if mode not in {"browser", "recognition"}:
            return {"ok": False, "message": "mode 必须是 browser/recognition"}
        result = self.save_settings({"detection_mode": mode})
        result["message"] = f"已切换全局检测模式为 {mode}"
        return result

    def set_scheduler_day_time(self, payload: dict[str, Any]) -> dict:
        try:
            weekday = int(payload.get("weekday", -1))
        except Exception:
            weekday = -1
        enabled = bool(payload.get("enabled", False))
        if weekday < 0 or weekday > 6:
            return {"ok": False, "message": "weekday 必须是 0-6"}
        with self._lock:
            config = self.load_config()
            scheduler_cfg = dict(config.get("scheduler", {}) or {})
            weekly_times = dict(scheduler_cfg.get("weekly_times", {}) or {})
            if enabled:
                existing_time = _normalize_time_value(weekly_times.get(str(weekday), "")) or "09:30"
                next_time = _normalize_time_value(payload.get("time", "")) or existing_time
                weekly_times[str(weekday)] = next_time
            else:
                weekly_times[str(weekday)] = None
            scheduler_cfg["weekly_times"] = weekly_times
            config["scheduler"] = scheduler_cfg
            self.save_config(config)
        self._refresh_monitoring_runtime()
        return {"ok": True, "message": f"已更新周{['一', '二', '三', '四', '五', '六', '日'][weekday]} 的自动查询时间"}

    def test_scheduler_notification_webhook(self, payload: dict[str, Any]) -> dict:
        webhook_url = str(payload.get("webhook_url", "") or "").strip()
        config = self.load_config()
        if _is_masked_secret(webhook_url):
            webhook_url = str(
                (config.get("scheduler", {}) or {}).get("notification_webhook_url", "") or ""
            ).strip()
        default_notification = config.get("default_notification", {}) or {}
        try:
            send_interval = max(1, int(default_notification.get("send_interval", 1) or 1))
        except Exception:
            send_interval = 1
        if not webhook_url:
            return {"ok": False, "message": "请先填写调度通知 webhook"}
        ok, error = send_scheduler_test_message(webhook_url, send_interval=send_interval)
        if ok:
            return {"ok": True, "message": "测试消息已发送，请到企业微信里确认是否收到。"}
        return {"ok": False, "message": error or "测试消息发送失败"}

    def set_ai_assistant_model(self, payload: dict[str, Any]) -> dict:
        platform = str(payload.get("platform", "") or "").strip()
        model = str(payload.get("model", "") or "").strip()
        if not platform or not model:
            return {"ok": False, "message": "缺少 platform 或 model"}
        result = self.save_settings({"ai_assistant": {"platform": _normalize_platform_id(platform), "model": model}})
        result["message"] = "已更新 AI 助手文本模型"
        return result

    def set_recognition_local_ocr(self, payload: dict[str, Any]) -> dict:
        result = self.save_settings({"recognition": {"safe_mode_ocr_enabled": True}})
        result["message"] = "识别模式已固定启用本地 OCR"
        return result

    def set_recognition_ai_fallback(self, payload: dict[str, Any]) -> dict:
        result = self.save_settings({"recognition": {}})
        result["message"] = "识别模式已固定为本地 OCR，AI 辅助设置已移除"
        return result

    def set_recognition_model(self, payload: dict[str, Any]) -> dict:
        result = self.save_settings({"recognition": {}})
        result["message"] = "识别模式已固定为本地 OCR，无需配置识别模型"
        return result

    def set_search_provider(self, payload: dict[str, Any]) -> dict:
        provider = str(payload.get("provider", "") or "").strip()
        if not provider:
            return {"ok": False, "message": "缺少 provider"}
        result = self.save_settings({"search": {"provider": provider}})
        result["message"] = f"已更新联网搜索提供方为 {provider}"
        return result

    def set_search_tavily_api_key(self, payload: dict[str, Any]) -> dict:
        result = self.save_settings({"search": {"tavily_api_key": str(payload.get("api_key", "") or "").strip()}})
        result["message"] = "已更新 Tavily API Key"
        return result

    def replace_search_model_pool(self, payload: dict[str, Any]) -> dict:
        result = self.save_settings({"search": {"model_pool": _normalize_model_options(payload.get("models", []))}})
        result["message"] = "已更新搜搜模型池"
        return result

    def add_search_model(self, payload: dict[str, Any]) -> dict:
        model = str(payload.get("model", "") or "").strip()
        if not model:
            return {"ok": False, "message": "缺少 model"}
        with self._lock:
            config = self.load_config()
            search_cfg = dict(config.get("search", {}) or {})
            models = _normalize_model_options(search_cfg.get("model_pool", []))
            if model not in models:
                models.append(model)
            config["search"] = {**search_cfg, "model_pool": models}
            self.save_config(config)
        return {"ok": True, "message": f"已将 {model} 加入搜搜模型池"}

    def remove_search_model(self, payload: dict[str, Any]) -> dict:
        model = str(payload.get("model", "") or "").strip()
        if not model:
            return {"ok": False, "message": "缺少 model"}
        with self._lock:
            config = self.load_config()
            search_cfg = dict(config.get("search", {}) or {})
            models = [item for item in _normalize_model_options(search_cfg.get("model_pool", [])) if item != model]
            config["search"] = {**search_cfg, "model_pool": models}
            self.save_config(config)
        return {"ok": True, "message": f"已从搜搜模型池移除 {model}"}

    def _save_screenshot_template_patch(self, payload: dict[str, Any], message: str) -> dict:
        del payload, message
        with self._lock:
            config = self.load_config()
            screenshot_cfg = dict(config.get("screenshot", {}) or {})
            screenshot_cfg["decoration"] = get_default_decoration_theme()
            config["screenshot"] = screenshot_cfg
            self.save_config(config)
        return {"ok": True, "message": "页面原始截图装饰模板已固定为默认样式"}

    def set_screenshot_template_basic(self, payload: dict[str, Any]) -> dict:
        return self._save_screenshot_template_patch(payload, "已更新截图模板基础文案")

    def set_screenshot_template_toggles(self, payload: dict[str, Any]) -> dict:
        return self._save_screenshot_template_patch(payload, "已更新截图模板显示开关")

    def set_screenshot_template_colors(self, payload: dict[str, Any]) -> dict:
        return self._save_screenshot_template_patch(payload, "已更新截图模板颜色")

    def set_screenshot_template_layout(self, payload: dict[str, Any]) -> dict:
        return self._save_screenshot_template_patch(payload, "已更新截图模板尺寸")

    def reset_screenshot_template(self, payload: dict[str, Any] | None = None) -> dict:
        with self._lock:
            config = self.load_config()
            screenshot_cfg = dict(config.get("screenshot", {}) or {})
            screenshot_cfg["decoration"] = get_default_decoration_theme()
            config["screenshot"] = screenshot_cfg
            self.save_config(config)
        return {"ok": True, "message": "页面原始截图装饰模板已固定为默认样式"}

    def set_profile_fields(self, payload: dict[str, Any]) -> dict:
        allowed = {key: payload[key] for key in ("name", "role", "avatar", "birthday", "hire_date") if key in payload}
        if not allowed:
            return {"ok": False, "message": "未提供资料字段"}
        result = self.save_profile(allowed)
        result["message"] = "已更新账号资料"
        return result

    def _resolve_task_id(self, task_id: str = "", task_name: str = "") -> str:
        config = self.load_config()
        for task in (config.get("tasks", []) or []):
            current_id = str(task.get("task_id") or derive_task_id(task)).strip()
            current_name = str(task.get("name") or current_id).strip()
            if task_id and current_id == str(task_id).strip():
                return current_id
            if task_name and current_name == str(task_name).strip():
                return current_id
        return ""

    def assistant_action(self, payload: dict) -> dict:
        action = str(payload.get("action", "") or "").strip()
        params = payload.get("params", {})
        if not action:
            return {"ok": False, "message": "缺少 action"}
        if not isinstance(params, dict):
            params = {}

        try:
            if action == "set_task_basic_info":
                result = self.set_task_basic_info(params)
            elif action == "set_task_tags":
                result = self.set_task_tags(params)
            elif action == "set_task_schedule":
                result = self.set_task_schedule(params)
            elif action == "set_task_runtime_options":
                result = self.set_task_runtime_options(params)
            elif action == "set_task_optimization_period":
                result = self.set_task_optimization_period(params)
            elif action == "set_task_platforms":
                result = self.set_task_platforms(params)
            elif action == "replace_task_keywords":
                result = self.replace_task_keywords(params)
            elif action == "add_task_keyword":
                result = self.add_task_keyword(params)
            elif action == "update_task_keyword":
                result = self.update_task_keyword(params)
            elif action == "remove_task_keyword":
                result = self.remove_task_keyword(params)
            elif action == "set_platform_api_key":
                result = self.set_platform_api_key(params)
            elif action == "set_platform_primary_model":
                result = self.set_platform_primary_model(params)
            elif action == "add_platform_model_option":
                result = self.add_platform_model_option(params)
            elif action == "remove_platform_model_option":
                result = self.remove_platform_model_option(params)
            elif action == "replace_platform_model_options":
                result = self.replace_platform_model_options(params)
            elif action == "test_platform_connection":
                result = self.test_platform_connection(params)
            elif action == "set_detection_mode":
                result = self.set_detection_mode(params)
            elif action == "set_scheduler_day_time":
                result = self.set_scheduler_day_time(params)
            elif action == "set_monitoring_enabled":
                result = self.set_monitoring_enabled(params)
            elif action == "set_ai_assistant_model":
                result = self.set_ai_assistant_model(params)
            elif action == "set_recognition_local_ocr":
                result = self.set_recognition_local_ocr(params)
            elif action == "set_recognition_ai_fallback":
                result = self.set_recognition_ai_fallback(params)
            elif action == "set_recognition_model":
                result = self.set_recognition_model(params)
            elif action == "set_search_provider":
                result = self.set_search_provider(params)
            elif action == "set_search_tavily_api_key":
                result = self.set_search_tavily_api_key(params)
            elif action == "replace_search_model_pool":
                result = self.replace_search_model_pool(params)
            elif action == "add_search_model":
                result = self.add_search_model(params)
            elif action == "remove_search_model":
                result = self.remove_search_model(params)
            elif action == "set_screenshot_template_basic":
                result = self.set_screenshot_template_basic(params)
            elif action == "set_screenshot_template_toggles":
                result = self.set_screenshot_template_toggles(params)
            elif action == "set_screenshot_template_colors":
                result = self.set_screenshot_template_colors(params)
            elif action == "set_screenshot_template_layout":
                result = self.set_screenshot_template_layout(params)
            elif action == "reset_screenshot_template":
                result = self.reset_screenshot_template(params)
            elif action == "set_profile_fields":
                result = self.set_profile_fields(params)
            elif action == "run_all_tasks":
                result = self.trigger_run_all()
            elif action == "run_selected_tasks":
                task_ids = list(params.get("task_ids") or [])
                task_names = list(params.get("task_names") or [])
                resolved_ids = [str(task_id).strip() for task_id in task_ids if str(task_id).strip()]
                for task_name in task_names:
                    resolved = self._resolve_task_id(task_name=str(task_name or "").strip())
                    if resolved and resolved not in resolved_ids:
                        resolved_ids.append(resolved)
                result = self.trigger_run_selected({"task_ids": resolved_ids})
            elif action == "create_task":
                result = self.create_task(params.get("payload") or params)
            elif action == "update_task":
                resolved = self._resolve_task_id(
                    task_id=str(params.get("task_id", "") or "").strip(),
                    task_name=str(params.get("task_name", "") or "").strip(),
                )
                if not resolved:
                    return {"ok": False, "message": "未找到要更新的任务"}
                result = self.update_task(resolved, params.get("payload") or {})
            elif action == "delete_task":
                resolved = self._resolve_task_id(
                    task_id=str(params.get("task_id", "") or "").strip(),
                    task_name=str(params.get("task_name", "") or "").strip(),
                )
                if not resolved:
                    return {"ok": False, "message": "未找到要删除的任务"}
                if not bool(params.get("confirm")):
                    return {"ok": False, "message": "删除任务需要明确确认"}
                result = self.delete_task(resolved)
            elif action == "test_run_task":
                resolved = self._resolve_task_id(
                    task_id=str(params.get("task_id", "") or "").strip(),
                    task_name=str(params.get("task_name", "") or "").strip(),
                )
                if not resolved:
                    return {"ok": False, "message": "未找到要测试的任务"}
                result = self.test_run_task(resolved)
            elif action == "save_profile":
                result = self.save_profile(params.get("payload") or params)
            elif action == "save_settings":
                result = self.save_settings(params.get("payload") or params)
            elif action == "save_platform_config":
                result = self.save_platform_config(params.get("payload") or params)
            elif action == "refresh_context_snapshots":
                result = self.refresh_context_snapshots(params.get("payload") or params)
            elif action == "recognition_action":
                result = self.recognition_action({"action": str(params.get("action", "") or "").strip()})
            elif action == "sync_todos":
                result = self.sync_todos({"todos": params.get("todos", [])})
            elif action == "import_article":
                result = self.import_article({"url": str(params.get("url", "") or "").strip()})
            elif action == "delete_article":
                result = self.delete_article(str(params.get("article_id", "") or "").strip())
            else:
                return {"ok": False, "message": f"不支持的 assistant action: {action}"}

            if isinstance(result, dict):
                return {
                    "ok": bool(result.get("ok", result.get("queued", False) or not result.get("message"))),
                    "action": action,
                    "result": result,
                    "snapshot": self.snapshot(),
                }
            return {"ok": True, "action": action, "result": result, "snapshot": self.snapshot()}
        except Exception as exc:
            return {"ok": False, "action": action, "message": str(exc)}

    # ── Run Selected ─────────────────────────────────────────────

    def trigger_run_selected(self, payload: dict) -> dict:
        """按 task_id 列表筛选并执行任务。"""
        blocked = self._viewer_execution_block_response()
        if blocked:
            return blocked
        task_ids = payload.get("task_ids", [])
        if not isinstance(task_ids, list) or not task_ids:
            return {"queued": False, "message": "请指定要运行的任务 ID"}
        if self._worker and self._worker.is_alive():
            return {"queued": False, "message": "已有任务正在执行"}

        task_id_set = set(str(tid).strip() for tid in task_ids)

        def worker() -> None:
            with self._lock:
                self._last_run = {
                    "startedAt": _local_iso_seconds(),
                    "status": "running",
                    "items": [],
                }
            items: list[dict] = []
            session_manager = None
            try:
                config = self.load_config()
                default_notification = config.get("default_notification", {}) or {}
                global_mode = config.get("detection_mode", "browser")
                manual_tasks: list[dict] = []
                for task in config.get("tasks", []) or []:
                    tid = task.get("task_id") or derive_task_id(task)
                    if tid not in task_id_set:
                        continue
                    manual_tasks.append(_apply_global_mode(task, global_mode))
                session_manager = self._create_query_session_manager(
                    manual_tasks,
                    config,
                    str(global_mode or "").strip(),
                    source_label="按选中任务执行",
                )
            except Exception as exc:
                with self._lock:
                    self._last_run = {
                        "startedAt": self._last_run.get("startedAt") if self._last_run else _local_iso_seconds(),
                        "finishedAt": _local_iso_seconds(),
                        "status": "failed",
                        "items": [{"taskName": "初始化", "ok": False, "error": str(exc)}],
                    }
                return
            for task in manual_tasks:
                task_name = str(task.get("name") or task.get("task_id") or derive_task_id(task)).strip()
                try:
                    result_items, report = run_task_group(
                        task, default_notification, config,
                        execution_source="manual", return_report=True,
                        platform_session_manager=session_manager,
                    )
                    items.append({"taskName": task_name, "ok": True, "queryCount": len(result_items), "report": report})
                except Exception as exc:
                    items.append({"taskName": task_name, "ok": False, "error": str(exc)})
            try:
                if session_manager is not None:
                    session_manager.close_all(reason="按选中任务执行结束")
            except Exception as exc:
                items.append({"taskName": "资源清理", "ok": False, "error": str(exc)})
            with self._lock:
                self._last_run = {
                    "startedAt": self._last_run.get("startedAt") if self._last_run else _local_iso_seconds(),
                    "finishedAt": _local_iso_seconds(),
                    "status": "done",
                    "items": items,
                }

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        return {"queued": True, "message": f"已开始执行 {len(task_id_set)} 个任务"}


def _safe_write_response(handler: BaseHTTPRequestHandler, data: bytes) -> None:
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        # 浏览器刷新、切页或主动取消请求时，客户端可能会先断开连接。
        # 这类情况不属于服务端业务错误，直接忽略即可，避免污染日志。
        return


def _safe_end_headers(handler: BaseHTTPRequestHandler) -> bool:
    try:
        handler.end_headers()
        return True
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        return False


def _json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = HTTPStatus.OK) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    _apply_cors_headers(handler)
    if not _safe_end_headers(handler):
        return
    _safe_write_response(handler, data)


def _text_response(handler: BaseHTTPRequestHandler, text: str, status: int = HTTPStatus.OK, content_type: str = "text/plain; charset=utf-8") -> None:
    data = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    _apply_cors_headers(handler)
    if not _safe_end_headers(handler):
        return
    _safe_write_response(handler, data)


def _bytes_response(handler: BaseHTTPRequestHandler, data: bytes, status: int = HTTPStatus.OK, content_type: str = "application/octet-stream") -> None:
    payload = data if isinstance(data, bytes) else bytes(data or b"")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "private, max-age=300")
    _apply_cors_headers(handler)
    if not _safe_end_headers(handler):
        return
    _safe_write_response(handler, payload)


def _download_file_response(handler: BaseHTTPRequestHandler, path: Path, file_name: str) -> None:
    data = path.read_bytes()
    content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    quoted_name = quote(file_name)
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted_name}")
    handler.send_header("Cache-Control", "no-store")
    _apply_cors_headers(handler)
    if not _safe_end_headers(handler):
        return
    _safe_write_response(handler, data)


def _stream_json_lines_response(
    handler: BaseHTTPRequestHandler,
    events: Any,
    *,
    status: int = HTTPStatus.OK,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("X-Accel-Buffering", "no")
    _apply_cors_headers(handler)
    if not _safe_end_headers(handler):
        return
    for event in events:
        payload = json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            handler.wfile.write(payload)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


class WebRequestHandler(BaseHTTPRequestHandler):
    server_version = get_http_server_version()

    @property
    def runtime(self) -> AppRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _send_internal_error(self, exc: Exception) -> None:
        method = str(getattr(self, "command", "") or "").strip() or "REQUEST"
        print(f"[WebBackend] {method} {self.path} 处理失败: {exc}")
        traceback.print_exc()
        try:
            _json_response(
                self,
                {
                    "ok": False,
                    "message": "服务端处理请求失败，请查看后端日志",
                    "error": exc.__class__.__name__,
                    "detail": str(exc),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return
        except Exception as response_exc:
            print(f"[WebBackend] 返回错误响应失败: {response_exc}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        try:
            _reject_disallowed_request(self)
        except _RequestRejected as exc:
            _json_response(self, {"ok": False, "message": exc.message}, status=exc.status)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        _apply_cors_headers(self)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {SESSION_TOKEN_HEADER}, X-CSRF-Token")
        _safe_end_headers(self)

    def do_GET(self) -> None:  # noqa: N802
        try:
            _reject_disallowed_request(self)
        except _RequestRejected as exc:
            _json_response(self, {"ok": False, "message": exc.message}, status=exc.status)
            return
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/session":
                _json_response(self, self.runtime.session_snapshot())
                return
            if path.startswith("/api/bootstrap"):
                _json_response(self, self.runtime.snapshot())
                return
            if path.startswith("/api/config"):
                _json_response(self, {"config": _sanitize_config_for_api(self.runtime.load_config())})
                return
            if path.startswith("/api/status"):
                _json_response(self, self.runtime.snapshot())
                return
            if path.startswith("/api/assets/profile/avatar"):
                asset = self.runtime.get_profile_avatar_asset()
                if asset is None:
                    _text_response(self, "avatar not found", status=HTTPStatus.NOT_FOUND)
                    return
                data, content_type = asset
                _bytes_response(self, data, content_type=content_type)
                return
            if path == "/api/search/download-file":
                qs = parse_qs(parsed.query)
                output_id = (qs.get("id", [""])[0] or "").strip()
                item = self.runtime.get_search_output_file(output_id)
                if not item:
                    _text_response(self, "file not found", status=HTTPStatus.NOT_FOUND)
                    return
                _download_file_response(self, Path(str(item["path"])), str(item["name"]))
                return
            if path == "/api/recognition/status":
                qs = parse_qs(parsed.query)
                compact = str((qs.get("compact", [""])[0] or "")).strip().lower() in {"1", "true", "yes"}
                passive = str((qs.get("passive", [""])[0] or "")).strip().lower() in {"1", "true", "yes"}
                _json_response(self, self.runtime.get_recognition_status(compact=compact, passive=passive))
                return
            exact_method_name = GET_EXACT_RUNTIME_METHODS.get(path)
            if exact_method_name:
                if path in GET_EXACT_SESSION_TOKEN_REQUIRED_PATHS:
                    try:
                        _reject_invalid_session_token(self)
                    except _RequestRejected as exc:
                        _json_response(self, {"ok": False, "message": exc.message}, status=exc.status)
                        return
                _json_response(self, getattr(self.runtime, exact_method_name)())
                return
            if self._handle_get_api_route(parsed, path):
                return
            self._serve_static(path)
        except Exception as exc:
            self._send_internal_error(exc)

    def _handle_get_api_route(self, parsed, path: str) -> bool:
        if path == "/api/dashboard/trend":
            qs = parse_qs(parsed.query)
            range_key = (qs.get("range", ["week"])[0] or "week").strip()
            _json_response(self, self.runtime.get_dashboard_trend(range_key))
            return True
        if path == "/api/aihot/daily-feed":
            _json_response(self, self.runtime.get_aihot_daily_feed())
            return True
        if path.startswith("/api/test-runs/"):
            run_id = path.replace("/api/test-runs/", "").strip()
            _json_response(self, self.runtime.get_test_run_status(run_id))
            return True
        if path.startswith("/api/batch-test/") and "/progress" in path:
            batch_id = path.replace("/api/batch-test/", "").replace("/progress", "")
            _json_response(self, self.runtime.get_batch_test_progress(batch_id))
            return True
        if path.startswith("/api/batch-test/") and "/report" in path:
            batch_id = path.replace("/api/batch-test/", "").replace("/report", "")
            _json_response(self, self.runtime.get_batch_test_report(batch_id))
            return True
        if path.startswith("/api/tasks/") and path.endswith("/monthly-stats"):
            task_id = path[len("/api/tasks/"):-len("/monthly-stats")]
            _json_response(self, self.runtime.get_task_monthly_stats(task_id))
            return True
        if path.startswith("/api/tasks/") and path.endswith("/article-reference-ranking"):
            qs = parse_qs(parsed.query)
            task_id = path[len("/api/tasks/"):-len("/article-reference-ranking")]
            platform = (qs.get("platform", ["all"])[0] or "all").strip()
            date_from = (qs.get("date_from", [""])[0] or "").strip()
            date_to = (qs.get("date_to", [""])[0] or "").strip()
            _json_response(
                self,
                self.runtime.get_task_article_reference_ranking(
                    task_id,
                    platform=platform,
                    date_from=date_from,
                    date_to=date_to,
                ),
            )
            return True
        if path.startswith("/api/tasks/") and path.endswith("/trend"):
            qs = parse_qs(parsed.query)
            range_key = (qs.get("range", ["week"])[0] or "week").strip()
            task_id = path[len("/api/tasks/"):-len("/trend")]
            _json_response(self, self.runtime.get_task_trend(task_id, range_key))
            return True
        if path == "/api/update/status":
            _json_response(self, self.runtime.check_app_update({}))
            return True
        if path == "/api/update/local-plan":
            qs = parse_qs(parsed.query)
            source_dir = (qs.get("source_dir", [""])[0] or "").strip()
            _json_response(self, self.runtime.get_local_update_plan({"source_dir": source_dir}))
            return True
        if path == "/api/articles":
            qs = parse_qs(parsed.query)
            media_type = (qs.get("type", [""])[0] or "").strip()
            limit = _safe_int(qs.get("limit", ["50"])[0], 50)
            task_name = (qs.get("task_name", [""])[0] or "").strip()
            include_export_keywords = str(
                qs.get("include_export_keywords", ["0"])[0] or "0"
            ).strip().lower() in {"1", "true", "yes", "on"}
            _json_response(
                self,
                self.runtime.get_articles_filtered(
                    media_type,
                    limit,
                    task_name,
                    include_export_keywords=include_export_keywords,
                ),
            )
            return True
        if path == "/api/articles/sqlite-shadow-compare":
            _json_response(self, self.runtime.get_article_sqlite_shadow_compare_status())
            return True
        if path == "/api/articles/import-batches":
            _json_response(self, self.runtime.get_pending_article_imports())
            return True
        if path == "/api/sync/export":
            qs = parse_qs(parsed.query)
            include_secrets = str((qs.get("include_secrets", ["0"])[0] or "0")).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if include_secrets:
                _reject_invalid_session_token(self)
            _json_response(self, self.runtime.export_sync_bundle(include_secrets=include_secrets))
            return True
        return False

    def do_POST(self) -> None:  # noqa: N802
        try:
            _reject_disallowed_request(self)
            _reject_invalid_session_token(self)
            self._do_POST()
        except _RequestRejected as exc:
            _json_response(self, {"ok": False, "message": exc.message}, status=exc.status)
        except Exception as exc:
            self._send_internal_error(exc)

    def _do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/actions/reload-config":
            _json_response(self, {"ok": True, "snapshot": self.runtime.snapshot()})
            return
        if path == "/api/actions/run-all":
            _json_response(self, self.runtime.trigger_run_all())
            return
        if path == "/api/selector-heal/diagnose":
            payload = self._read_json()
            _json_response(self, self.runtime.diagnose_selector_heal(payload))
            return
        if path == "/api/selector-heal/apply":
            payload = self._read_json()
            _json_response(self, self.runtime.apply_selector_heal(payload))
            return
        if path == "/api/selector-heal/pause-state":
            payload = self._read_json()
            _json_response(self, self.runtime.diagnose_selector_pause_state(payload))
            return
        exact_method_name = POST_JSON_RUNTIME_METHODS.get(path)
        if exact_method_name:
            payload = self._read_json()
            _json_response(self, getattr(self.runtime, exact_method_name)(payload))
            return
        if self._handle_post_dynamic_api_route(parsed, path):
            return
        _json_response(self, {"ok": False, "message": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _handle_post_dynamic_api_route(self, parsed, path: str) -> bool:
        if path == "/api/search/upload-file":
            qs = parse_qs(parsed.query)
            file_name = (qs.get("name", ["uploaded.xlsx"])[0] or "uploaded.xlsx").strip()
            data = self._read_binary(MAX_SEARCH_UPLOAD_BYTES)
            _json_response(self, self.runtime.save_search_upload(file_name, data))
            return True
        if path == "/api/keywords/import-file":
            qs = parse_qs(parsed.query)
            file_name = (qs.get("name", ["keywords.xlsx"])[0] or "keywords.xlsx").strip()
            data = self._read_binary(MAX_SEARCH_UPLOAD_BYTES)
            _json_response(self, self.runtime.import_keywords_from_file(file_name, data))
            return True
        if path == "/api/articles/import-file":
            qs = parse_qs(parsed.query)
            file_name = (qs.get("name", ["articles.xlsx"])[0] or "articles.xlsx").strip()
            data = self._read_binary(MAX_SEARCH_UPLOAD_BYTES)
            _json_response(self, self.runtime.import_articles_from_file(file_name, data))
            return True
        if path.startswith("/api/articles/import-batches/") and path.endswith("/confirm"):
            import_id = path[len("/api/articles/import-batches/"):-len("/confirm")].strip("/")
            _json_response(self, self.runtime.confirm_article_import(import_id))
            return True
        if path.startswith("/api/articles/import-batches/") and path.endswith("/undo"):
            import_id = path[len("/api/articles/import-batches/"):-len("/undo")].strip("/")
            _json_response(self, self.runtime.undo_article_import(import_id))
            return True
        if path.startswith("/api/platforms/") and path.endswith("/test"):
            platform_id = path.replace("/api/platforms/", "").replace("/test", "")
            payload = self._read_json()
            _json_response(self, self.runtime.test_platform(platform_id, payload))
            return True
        if path.startswith("/api/tasks/") and path.endswith("/test-run/start"):
            task_id = path.replace("/api/tasks/", "").replace("/test-run/start", "")
            print(f"[WebBackend] 收到品牌测试启动请求: task_id={task_id}")
            _json_response(self, self.runtime.start_test_run_task(task_id))
            return True
        if path.startswith("/api/test-runs/") and path.endswith("/cancel"):
            run_id = path.replace("/api/test-runs/", "").replace("/cancel", "").strip()
            _json_response(self, self.runtime.cancel_test_run(run_id))
            return True
        if path.startswith("/api/tasks/") and path.endswith("/articles/export-wecom"):
            task_id = path.replace("/api/tasks/", "").replace("/articles/export-wecom", "")
            payload = self._read_json()
            _json_response(self, self.runtime.export_task_articles_to_wecom(task_id, payload))
            return True
        if path.startswith("/api/tasks/") and path.endswith("/force-send-success"):
            task_id = path.replace("/api/tasks/", "").replace("/force-send-success", "")
            _json_response(self, self.runtime.force_send_successful_task_results(task_id))
            return True
        if path.startswith("/api/tasks/") and path.endswith("/test-run"):
            task_id = path.replace("/api/tasks/", "").replace("/test-run", "")
            print(f"[WebBackend] 收到品牌测试请求: task_id={task_id}")
            _json_response(self, self.runtime.test_run_task(task_id))
            return True
        if path.startswith("/api/batch-test/") and path.endswith("/cancel"):
            batch_id = path.replace("/api/batch-test/", "").replace("/cancel", "")
            _json_response(self, self.runtime.cancel_batch_test(batch_id))
            return True
        if path == "/api/chat/stream":
            payload = self._read_json()
            _stream_json_lines_response(self, self.runtime.chat_stream(payload))
            return True
        return False

    def do_PUT(self) -> None:  # noqa: N802
        try:
            _reject_disallowed_request(self)
            _reject_invalid_session_token(self)
            self._do_PUT()
        except _RequestRejected as exc:
            _json_response(self, {"ok": False, "message": exc.message}, status=exc.status)
        except Exception as exc:
            self._send_internal_error(exc)

    def _do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if self._handle_put_dynamic_api_route(path):
            return
        _json_response(self, {"ok": False, "message": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _handle_put_dynamic_api_route(self, path: str) -> bool:
        for prefix, method_name in PUT_DYNAMIC_RUNTIME_METHODS:
            if path.startswith(prefix):
                item_id = path.replace(prefix, "")
                payload = self._read_json()
                _json_response(self, getattr(self.runtime, method_name)(item_id, payload))
                return True
        if path.startswith("/api/articles/"):
            article_id = path.replace("/api/articles/", "")
            payload = self._read_json()
            payload_keys = {str(key) for key in payload.keys()}
            if payload_keys and payload_keys <= {"media_type"}:
                _json_response(
                    self,
                    self.runtime.update_article_media_type(
                        article_id,
                        str(payload.get("media_type", "") or "").strip(),
                    ),
                )
                return True
            _json_response(
                self,
                self.runtime.update_article(article_id, payload),
            )
            return True
        return False

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            _reject_disallowed_request(self)
            _reject_invalid_session_token(self)
            self._do_DELETE()
        except _RequestRejected as exc:
            _json_response(self, {"ok": False, "message": exc.message}, status=exc.status)
        except Exception as exc:
            self._send_internal_error(exc)

    def _do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/articles/"):
            article_id = path.replace("/api/articles/", "")
            qs = parse_qs(parsed.query)
            task_name = (qs.get("task_name", [""])[0] or "").strip()
            _json_response(self, self.runtime.delete_article(article_id, task_name))
            return
        if self._handle_delete_dynamic_api_route(path):
            return
        _json_response(self, {"ok": False, "message": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _handle_delete_dynamic_api_route(self, path: str) -> bool:
        for prefix, method_name in DELETE_DYNAMIC_RUNTIME_METHODS:
            if path.startswith(prefix):
                item_id = path.replace(prefix, "")
                _json_response(self, getattr(self.runtime, method_name)(item_id))
                return True
        return False

    def _read_json(self) -> dict:
        length = _safe_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        if length > MAX_JSON_BODY_BYTES:
            raise _RequestRejected(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON payload too large")
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except UnicodeDecodeError:
            print("[WebBackend] 收到非 UTF-8 JSON 请求体，已忽略")
            return {}
        except json.JSONDecodeError:
            print("[WebBackend] 收到无效 JSON 请求体，已忽略")
            return {}

    def _read_binary(self, max_bytes: int) -> bytes:
        length = _safe_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return b""
        if length > max_bytes:
            raise _RequestRejected(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "uploaded file too large")
        return self.rfile.read(length)

    def _serve_static(self, request_path: str) -> None:
        dist_root = self.runtime.frontend_dist
        if not dist_root.exists():
            _text_response(
                self,
                "Frontend has not been built yet. Run `npm install` and `npm run build` in web-ui/.",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        relative = request_path.lstrip("/")
        if not relative or request_path == "/":
            relative = "index.html"
        elif "." not in Path(relative).name:
            relative = "index.html"

        file_path = (dist_root / relative).resolve()
        try:
            file_path.relative_to(dist_root.resolve())
        except Exception:
            file_path = dist_root / "index.html"

        if not file_path.exists() or file_path.is_dir():
            file_path = dist_root / "index.html"

        try:
            data = file_path.read_bytes()
        except Exception as exc:
            _text_response(self, f"Failed to read frontend asset: {exc}", status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if file_path.name == "index.html":
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        _apply_cors_headers(self)
        if not _safe_end_headers(self):
            return
        _safe_write_response(self, data)


class WebAppServer:
    """Manage the local HTTP server used by the desktop shell."""

    def __init__(self) -> None:
        self.runtime = AppRuntime()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _pick_port() -> int:
        # 优先使用固定端口 51082（与 vite dev proxy 对齐），被占用时回退到随机端口
        preferred = 51082
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start(self) -> str:
        if self._server:
            return self.url

        port = self._pick_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), WebRequestHandler)
        server.runtime = self.runtime  # type: ignore[attr-defined]
        self.runtime._server = server
        self.runtime.port = port
        self._server = server
        self.runtime._start_cloud_command_transport()

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._thread = thread
        self.runtime.restore_monitoring_if_needed()
        self.runtime.schedule_context_snapshot_startup_refresh()
        # 注释掉预热：ImageGrab.grabclipboard() 会在子线程触发 tkinter 初始化，macOS 不允许
        # self.runtime.schedule_recognition_warmup()
        return self.url

    def stop(self) -> None:
        self.runtime.shutdown()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        if not self.runtime.port:
            return "http://127.0.0.1:0/"
        return f"http://127.0.0.1:{self.runtime.port}/"


def create_server() -> WebAppServer:
    return WebAppServer()


if __name__ == "__main__":
    from core.windows_bootstrap import install_windows_bootstrap
    install_windows_bootstrap()

    import time as _time
    server = create_server()
    _server_stop_lock = threading.Lock()
    _server_stopped = False

    def _stop_server_once() -> None:
        global _server_stopped
        with _server_stop_lock:
            if _server_stopped:
                return
            _server_stopped = True
        server.stop()

    register_shutdown_callback("web-backend.server", _stop_server_once)
    install_shutdown_handlers()
    url = server.start()
    print(f"[WebBackend] 后端已启动: {url}")
    print("[WebBackend] 前端请访问 http://localhost:5173  (需另开终端跑 npm run dev)")
    print("[WebBackend] Ctrl+C 停止")
    try:
        while True:
            _time.sleep(1)
    except KeyboardInterrupt:
        run_shutdown_callbacks("keyboard interrupt")
        print("[WebBackend] 已停止")
