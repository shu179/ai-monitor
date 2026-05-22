"""Helpers for exporting/importing sync-friendly local data bundles."""

from __future__ import annotations

import copy
from typing import Any

from .article_store import export_article_store_bundle, import_article_store_bundle
from .config_watcher import save_config
from .daily_task_state import assign_task_id
from .monitoring_sync import export_monitoring_sync_bundle, import_monitoring_sync_bundle
from .quick_todos import normalize_quick_todos
from .time_utils import local_now

SYNC_SCHEMA_VERSION = "ai-monitor.sync.v1"

_SYNC_SETTINGS_SECTIONS = (
    "scheduler",
    "ai_assistant",
    "recognition",
    "search",
    "default_notification",
    "query_execution",
    "screenshot",
    "browser_automation",
    "app_update",
)

_SYNC_PLATFORM_FIELDS = (
    "enabled",
    "api_model",
    "model_options",
)

_SECRET_SCHEDULER_FIELDS = {"notification_webhook_url"}
_SECRET_SEARCH_FIELDS = {"tavily_api_key"}
_SECRET_PLATFORM_FIELDS = {"api_key"}
_SECRET_TASK_FIELDS = {"webhook_url"}


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base or {})
    for key, value in (override or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _export_platforms(platforms_cfg: dict[str, Any] | None, *, include_secrets: bool) -> dict[str, dict[str, Any]]:
    exported: dict[str, dict[str, Any]] = {}
    for raw_platform_id, raw_platform_cfg in (platforms_cfg or {}).items():
        if not isinstance(raw_platform_cfg, dict):
            continue
        platform_id = str(raw_platform_id or "").strip()
        if not platform_id:
            continue
        item: dict[str, Any] = {}
        for field in _SYNC_PLATFORM_FIELDS:
            if field not in raw_platform_cfg:
                continue
            item[field] = copy.deepcopy(raw_platform_cfg.get(field))
        if include_secrets and str(raw_platform_cfg.get("api_key", "") or "").strip():
            item["api_key"] = str(raw_platform_cfg.get("api_key", "") or "").strip()
        if item:
            exported[platform_id] = item
    return exported


def _export_settings(config: dict[str, Any], *, include_secrets: bool) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for section in _SYNC_SETTINGS_SECTIONS:
        value = config.get(section)
        if isinstance(value, dict):
            item = copy.deepcopy(value)
            if section == "scheduler" and not include_secrets:
                for field in _SECRET_SCHEDULER_FIELDS:
                    item.pop(field, None)
            if section == "search" and not include_secrets:
                for field in _SECRET_SEARCH_FIELDS:
                    item.pop(field, None)
            settings[section] = item
    settings["platforms"] = _export_platforms(config.get("platforms", {}), include_secrets=include_secrets)
    settings["detection_mode"] = str(config.get("detection_mode", "browser") or "browser").strip() or "browser"
    return settings


def _export_tasks(tasks: list[Any] | None, *, include_secrets: bool) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for raw_task in tasks or []:
        if not isinstance(raw_task, dict):
            continue
        item = copy.deepcopy(raw_task)
        assign_task_id(item)
        if not include_secrets:
            for field in _SECRET_TASK_FIELDS:
                item.pop(field, None)
        exported.append(item)
    return exported


def _normalize_bundle_payload(payload: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    raw = payload if isinstance(payload, dict) else {}
    bundle = raw.get("bundle") if isinstance(raw.get("bundle"), dict) else None
    source = bundle if isinstance(bundle, dict) else raw
    data = source.get("data") if isinstance(source.get("data"), dict) else source
    include_secrets = bool(source.get("includeSecrets", raw.get("includeSecrets", False)))
    return data if isinstance(data, dict) else {}, include_secrets


def build_sync_bundle(config: dict[str, Any] | None, *, include_secrets: bool = False) -> dict[str, Any]:
    current_config = copy.deepcopy(config or {})
    todos, _ = normalize_quick_todos(current_config.get("quick_todos", []))
    article_bundle = export_article_store_bundle()
    monitoring_bundle = export_monitoring_sync_bundle()
    tasks = _export_tasks(current_config.get("tasks", []), include_secrets=include_secrets)
    profile = copy.deepcopy(current_config.get("profile", {}) or {})
    settings = _export_settings(current_config, include_secrets=include_secrets)

    return {
        "schemaVersion": SYNC_SCHEMA_VERSION,
        "exportedAt": local_now().isoformat(timespec="seconds"),
        "includeSecrets": bool(include_secrets),
        "data": {
            "profile": profile,
            "tasks": tasks,
            "settings": settings,
            "todos": todos,
            "articles": list(article_bundle.get("articles", []) or []),
            "articleMemory": {
                "domain_overrides": dict(article_bundle.get("domain_overrides", {}) or {}),
                "domain_media_names": dict(article_bundle.get("domain_media_names", {}) or {}),
                "excluded_article_urls": dict(article_bundle.get("excluded_article_urls", {}) or {}),
            },
            "monitoring": monitoring_bundle,
        },
        "stats": {
            "taskCount": len(tasks),
            "todoCount": len(todos),
            "articleCount": len(article_bundle.get("articles", []) or []),
            "excludedArticleUrlCount": len(article_bundle.get("excluded_article_urls", {}) or {}),
            "historyFileCount": len(monitoring_bundle.get("history_files", {}) or {}),
            "dailyTaskStatusCount": len(monitoring_bundle.get("daily_task_status", {}) or {}),
            "schedulerStateCount": len(monitoring_bundle.get("scheduler_state", {}) or {}),
        },
    }


def _normalize_mode(value: Any) -> str:
    mode = str(value or "merge").strip().lower()
    return mode if mode in {"merge", "replace"} else "merge"


def _prepare_tasks(items: list[Any] | None) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for raw_item in items or []:
        if not isinstance(raw_item, dict):
            continue
        item = copy.deepcopy(raw_item)
        assign_task_id(item)
        prepared.append(item)
    return prepared


def _merge_tasks(existing: list[Any] | None, incoming: list[Any] | None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    result = [copy.deepcopy(item) for item in (existing or []) if isinstance(item, dict)]
    created = 0
    updated = 0

    index_by_id = {
        str(task.get("task_id", "")).strip(): index
        for index, task in enumerate(result)
        if str(task.get("task_id", "")).strip()
    }
    index_by_name = {
        str(task.get("name", "")).strip(): index
        for index, task in enumerate(result)
        if str(task.get("name", "")).strip()
    }

    for task in _prepare_tasks(incoming):
        task_id = str(task.get("task_id", "")).strip()
        task_name = str(task.get("name", "")).strip()
        target_index = None
        if task_id:
            target_index = index_by_id.get(task_id)
        if target_index is None and task_name:
            target_index = index_by_name.get(task_name)

        if target_index is None:
            result.append(task)
            created += 1
            target_index = len(result) - 1
        else:
            merged = copy.deepcopy(result[target_index])
            merged.update(task)
            merged["task_id"] = task_id or str(merged.get("task_id", "")).strip()
            result[target_index] = merged
            updated += 1

        final_task = result[target_index]
        final_task_id = str(final_task.get("task_id", "")).strip()
        final_task_name = str(final_task.get("name", "")).strip()
        if final_task_id:
            index_by_id[final_task_id] = target_index
        if final_task_name:
            index_by_name[final_task_name] = target_index

    return result, {"created": created, "updated": updated, "total": len(result)}


def _replace_tasks(incoming: list[Any] | None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    tasks = _prepare_tasks(incoming)
    return tasks, {"created": len(tasks), "updated": 0, "total": len(tasks)}


def _merge_todos(existing: list[Any] | None, incoming: list[Any] | None, *, mode: str) -> list[dict[str, Any]]:
    incoming_normalized, _ = normalize_quick_todos(list(incoming or []))
    if mode == "replace":
        return incoming_normalized

    existing_normalized, _ = normalize_quick_todos(list(existing or []))
    result = [copy.deepcopy(item) for item in existing_normalized]
    index_by_id = {
        str(item.get("id", "")).strip(): index
        for index, item in enumerate(result)
        if str(item.get("id", "")).strip()
    }
    index_by_text = {
        str(item.get("text", "")).strip(): index
        for index, item in enumerate(result)
        if str(item.get("text", "")).strip()
    }

    for item in incoming_normalized:
        todo_id = str(item.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        target_index = index_by_id.get(todo_id) if todo_id else None
        if target_index is None and text:
            target_index = index_by_text.get(text)
        if target_index is None:
            result.append(copy.deepcopy(item))
            target_index = len(result) - 1
        else:
            merged = copy.deepcopy(result[target_index])
            merged.update(item)
            result[target_index] = merged
        final_item = result[target_index]
        final_id = str(final_item.get("id", "")).strip()
        final_text = str(final_item.get("text", "")).strip()
        if final_id:
            index_by_id[final_id] = target_index
        if final_text:
            index_by_text[final_text] = target_index

    normalized_result, _ = normalize_quick_todos(result)
    return normalized_result


def _merge_platforms(existing: dict[str, Any] | None, incoming: dict[str, Any] | None, *, mode: str) -> dict[str, Any]:
    current = copy.deepcopy(existing or {})
    patches = incoming if isinstance(incoming, dict) else {}
    if mode == "replace":
        next_platforms: dict[str, Any] = {}
    else:
        next_platforms = current

    for raw_platform_id, raw_platform_cfg in patches.items():
        if not isinstance(raw_platform_cfg, dict):
            continue
        platform_id = str(raw_platform_id or "").strip()
        if not platform_id:
            continue
        current_item = copy.deepcopy(next_platforms.get(platform_id, {}) or {})
        for field in _SYNC_PLATFORM_FIELDS:
            if field in raw_platform_cfg:
                current_item[field] = copy.deepcopy(raw_platform_cfg.get(field))
        if "api_key" in raw_platform_cfg:
            current_item["api_key"] = str(raw_platform_cfg.get("api_key", "") or "").strip()
        next_platforms[platform_id] = current_item

    return next_platforms


def _preserve_unsynced_secrets(existing_config: dict[str, Any], next_config: dict[str, Any]) -> dict[str, Any]:
    preserved = copy.deepcopy(next_config or {})

    existing_scheduler = existing_config.get("scheduler", {}) or {}
    current_scheduler = preserved.get("scheduler", {}) or {}
    if isinstance(existing_scheduler, dict) and isinstance(current_scheduler, dict):
        secret = str(existing_scheduler.get("notification_webhook_url", "") or "").strip()
        if secret and not str(current_scheduler.get("notification_webhook_url", "") or "").strip():
            current_scheduler["notification_webhook_url"] = secret
            preserved["scheduler"] = current_scheduler

    existing_search = existing_config.get("search", {}) or {}
    current_search = preserved.get("search", {}) or {}
    if isinstance(existing_search, dict) and isinstance(current_search, dict):
        secret = str(existing_search.get("tavily_api_key", "") or "").strip()
        if secret and not str(current_search.get("tavily_api_key", "") or "").strip():
            current_search["tavily_api_key"] = secret
            preserved["search"] = current_search

    existing_platforms = existing_config.get("platforms", {}) or {}
    current_platforms = preserved.get("platforms", {}) or {}
    if isinstance(existing_platforms, dict) and isinstance(current_platforms, dict):
        for platform_id, platform_cfg in current_platforms.items():
            if not isinstance(platform_cfg, dict):
                continue
            existing_platform_cfg = existing_platforms.get(platform_id, {}) or {}
            if not isinstance(existing_platform_cfg, dict):
                continue
            secret = str(existing_platform_cfg.get("api_key", "") or "").strip()
            if secret and not str(platform_cfg.get("api_key", "") or "").strip():
                platform_cfg["api_key"] = secret

    existing_tasks = [item for item in (existing_config.get("tasks", []) or []) if isinstance(item, dict)]
    task_secret_by_id = {
        str(task.get("task_id", "")).strip(): str(task.get("webhook_url", "") or "").strip()
        for task in existing_tasks
        if str(task.get("task_id", "")).strip()
    }
    task_secret_by_name = {
        str(task.get("name", "")).strip(): str(task.get("webhook_url", "") or "").strip()
        for task in existing_tasks
        if str(task.get("name", "")).strip()
    }
    for task in preserved.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", "")).strip()
        task_name = str(task.get("name", "")).strip()
        secret = ""
        if task_id:
            secret = task_secret_by_id.get(task_id, "")
        if not secret and task_name:
            secret = task_secret_by_name.get(task_name, "")
        if secret and not str(task.get("webhook_url", "") or "").strip():
            task["webhook_url"] = secret

    return preserved


def prepare_sync_import(existing_config: dict[str, Any] | None, payload: dict[str, Any] | None, *, mode: str = "merge") -> dict[str, Any]:
    current_config = copy.deepcopy(existing_config or {})
    data, include_secrets = _normalize_bundle_payload(payload)
    normalized_mode = _normalize_mode(mode)
    next_config = copy.deepcopy(current_config)
    summary: dict[str, Any] = {
        "mode": normalized_mode,
        "includeSecrets": include_secrets,
    }

    if isinstance(data.get("profile"), dict):
        if normalized_mode == "replace":
            next_config["profile"] = copy.deepcopy(data.get("profile") or {})
        else:
            next_config["profile"] = _deep_merge_dict(
                next_config.get("profile", {}) or {},
                data.get("profile") or {},
            )
        summary["profileUpdated"] = True
    else:
        summary["profileUpdated"] = False

    incoming_settings = data.get("settings")
    if isinstance(incoming_settings, dict):
        for section in _SYNC_SETTINGS_SECTIONS:
            if section not in incoming_settings or not isinstance(incoming_settings.get(section), dict):
                continue
            if normalized_mode == "replace":
                next_config[section] = copy.deepcopy(incoming_settings.get(section) or {})
            else:
                next_config[section] = _deep_merge_dict(
                    next_config.get(section, {}) or {},
                    incoming_settings.get(section) or {},
                )
        if "detection_mode" in incoming_settings:
            next_config["detection_mode"] = str(incoming_settings.get("detection_mode", "") or "").strip() or "browser"
        if "platforms" in incoming_settings:
            next_config["platforms"] = _merge_platforms(
                next_config.get("platforms", {}) or {},
                incoming_settings.get("platforms") or {},
                mode=normalized_mode,
            )
        summary["settingsUpdated"] = True
    else:
        summary["settingsUpdated"] = False

    if "tasks" in data:
        if normalized_mode == "replace":
            next_tasks, task_summary = _replace_tasks(data.get("tasks"))
        else:
            next_tasks, task_summary = _merge_tasks(next_config.get("tasks", []), data.get("tasks"))
        next_config["tasks"] = next_tasks
        summary["tasks"] = task_summary
    else:
        summary["tasks"] = {"created": 0, "updated": 0, "total": len(next_config.get("tasks", []) or [])}

    if "todos" in data:
        next_todos = _merge_todos(next_config.get("quick_todos", []), data.get("todos"), mode=normalized_mode)
        next_config["quick_todos"] = next_todos
        summary["todos"] = {"total": len(next_todos)}
    else:
        summary["todos"] = {"total": len(next_config.get("quick_todos", []) or [])}

    if not include_secrets:
        next_config = _preserve_unsynced_secrets(current_config, next_config)

    article_bundle = None
    if "articles" in data or "articleMemory" in data:
        article_memory = data.get("articleMemory") if isinstance(data.get("articleMemory"), dict) else {}
        article_bundle = {
            "articles": list(data.get("articles", []) or []),
            "domain_overrides": dict(article_memory.get("domain_overrides", {}) or {}),
            "domain_media_names": dict(article_memory.get("domain_media_names", {}) or {}),
            "excluded_article_urls": dict(article_memory.get("excluded_article_urls", {}) or {}),
        }

    summary["articlesRequested"] = article_bundle is not None
    monitoring_bundle = data.get("monitoring") if isinstance(data.get("monitoring"), dict) else None
    summary["monitoringRequested"] = monitoring_bundle is not None
    return {
        "config": next_config,
        "article_bundle": article_bundle,
        "monitoring_bundle": monitoring_bundle,
        "summary": summary,
        "mode": normalized_mode,
        "includeSecrets": include_secrets,
    }


def apply_sync_bundle(
    config_path,
    existing_config: dict[str, Any] | None,
    payload: dict[str, Any] | None,
    *,
    mode: str = "merge",
) -> dict[str, Any]:
    prepared = prepare_sync_import(existing_config, payload, mode=mode)
    next_config = prepared.get("config", existing_config or {})
    normalized_mode = str(prepared.get("mode", "merge") or "merge").strip().lower()

    save_config(next_config, str(config_path))

    article_summary = None
    article_bundle = prepared.get("article_bundle")
    if isinstance(article_bundle, dict):
        article_summary = import_article_store_bundle(article_bundle, mode=normalized_mode)

    monitoring_summary = None
    monitoring_bundle = prepared.get("monitoring_bundle")
    if isinstance(monitoring_bundle, dict):
        monitoring_summary = import_monitoring_sync_bundle(monitoring_bundle, mode=normalized_mode)

    result_summary = dict(prepared.get("summary", {}) or {})
    if article_summary is not None:
        result_summary["articles"] = article_summary
    if monitoring_summary is not None:
        result_summary["monitoring"] = monitoring_summary

    return {
        "ok": True,
        "message": "同步包已导入",
        "mode": normalized_mode,
        "summary": result_summary,
        "config": next_config,
    }
