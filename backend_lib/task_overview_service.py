"""Task overview payload service for the local web backend."""

from __future__ import annotations

import copy
import threading
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from backend_lib.config_provider import RuntimeConfigProvider
from backend_lib.dashboard_tasks import (
    _build_task_failure_summary,
    _collect_today_successful_task_payload,
    _compute_fixed_screenshot_target,
    _task_is_scheduled_for_day,
)
from core.daily_task_state import derive_task_id, get_task_day_status, get_task_status_label
from core.history import (
    get_brand_trend_series,
    get_records,
    get_task_brand_names,
    is_manual_test_failure_record,
    is_success_record,
)
from core.scheduler import describe_task_schedule
from core.time_utils import local_today


class TaskOverviewService:
    """Builds the `/api/tasks/full` payload and owns its short-lived cache."""

    def __init__(
        self,
        *,
        config_provider: RuntimeConfigProvider,
        synced_articles_loader: Callable[[dict[str, Any]], list[dict[str, Any]]],
        prune_test_failure_notices: Callable[[], None],
        test_failure_notice_getter: Callable[[str], dict[str, Any] | None],
        platform_display_name: Callable[[str], str],
        secret_masker: Callable[[Any], str],
        normalize_string_list: Callable[[Any], list[str]],
        cache_ttl_seconds: float,
    ) -> None:
        self._config_provider = config_provider
        self._synced_articles_loader = synced_articles_loader
        self._prune_test_failure_notices = prune_test_failure_notices
        self._test_failure_notice_getter = test_failure_notice_getter
        self._platform_display_name = platform_display_name
        self._secret_masker = secret_masker
        self._normalize_string_list = normalize_string_list
        self._cache_ttl_seconds = float(cache_ttl_seconds or 0.0)
        self._cache_lock = threading.RLock()
        self._cache: dict[str, Any] | None = None

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache = None

    def get_tasks_full(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache
            if cached and now - float(cached.get("updated_at", 0.0) or 0.0) < self._cache_ttl_seconds:
                payload = cached.get("payload")
                if isinstance(payload, dict):
                    return copy.deepcopy(payload)

        payload = self._build_tasks_full_payload()
        with self._cache_lock:
            self._cache = {
                "updated_at": time.monotonic(),
                "payload": copy.deepcopy(payload),
            }
        return payload

    def _build_tasks_full_payload(self) -> dict[str, Any]:
        config = self._config_provider.load()
        active_mode = str(config.get("detection_mode", "browser") or "browser").strip()
        self._prune_test_failure_notices()
        tasks = [
            task
            for task in list(config.get("tasks", []) or [])
            if isinstance(task, dict) and not _is_revoked_cloud_task(task)
        ]
        article_count_by_task: Counter[str] = Counter()
        try:
            all_articles = self._synced_articles_loader(config)
            for article in all_articles:
                for matched_task in (article.get("matched_tasks") or []):
                    matched_name = str(matched_task or "").strip()
                    if matched_name:
                        article_count_by_task[matched_name] += 1
        except Exception:
            article_count_by_task = Counter()

        result = []
        for task in tasks:
            task_id = task.get("task_id") or derive_task_id(task)
            task_name = str(task.get("name") or task_id).strip()
            brand_names = get_task_brand_names(task)
            status = get_task_day_status(task)
            scheduled_today = _task_is_scheduled_for_day(task, config.get("scheduler", {}), local_today())
            failure_summary = _build_task_failure_summary(
                task,
                day_status_loader=get_task_day_status,
                platform_display_name=self._platform_display_name,
            )
            current_status = str(status.get("status") or "pending").strip()
            brand_status = str(status.get("brand_status") or "").strip()
            test_failure_notice = self._test_failure_notice_getter(str(task_id))
            success_progress = _collect_today_successful_task_payload(
                task,
                day_status_loader=get_task_day_status,
                normalize_string_list=self._normalize_string_list,
            )
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
            actual_screenshot_count_today = int(success_progress.get("actualScreenshotCount") or 0)
            fixed_screenshot_target_today = _compute_fixed_screenshot_target(task)
            completed_by_quota_today = (
                fixed_screenshot_target_today > 0
                and actual_screenshot_count_today >= fixed_screenshot_target_today
            )

            all_platforms = sorted({
                self._platform_display_name(p)
                for kw in (task.get("keywords") or [])
                for p in (kw.get("platforms") or [])
                if str(p).strip()
            })

            try:
                records = get_records(task_name, task_id=str(task.get("task_id") or derive_task_id(task)).strip())
            except Exception:
                records = []
            records_for_stats = [record for record in records if not is_manual_test_failure_record(record)]
            total_records = len(records_for_stats)
            success_records = sum(1 for record in records_for_stats if is_success_record(record))

            article_count = int(article_count_by_task.get(task_name, 0))

            try:
                trend_series = get_brand_trend_series(
                    task_name,
                    brand_names,
                    10,
                    task_id=str(task.get("task_id") or derive_task_id(task)).strip(),
                    task_created_at=str(
                        task.get("created_at")
                        or task.get("createdAt")
                        or task.get("cloud_created_at")
                        or task.get("cloud_synced_at")
                        or ""
                    ).strip(),
                ) or {}
                actual_values = [float(value) for value in (trend_series.get("actual") or []) if value is not None]
                optimization_trend = [
                    {"value": round(value, 1)}
                    for value in actual_values[-10:]
                ]
            except Exception:
                optimization_trend = []

            result.append({
                "id": task_id,
                "name": task_name,
                "brand": str(task.get("brand", "") or "").strip(),
                "enabled": task.get("enabled", True),
                "mode": active_mode,
                "schedule": describe_task_schedule(task, config.get("scheduler", {})),
                "status": current_status or "pending",
                "status_label": get_task_status_label(current_status or "pending"),
                "brand_status": brand_status or "pending",
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
                "failed_today": bool(failure_summary.get("failedToday")),
                "failed_modes_today": list(failure_summary.get("failedModes") or []),
                "failed_updated_at": str(failure_summary.get("failedUpdatedAt") or ""),
                "failure_kind_today": str(failure_summary.get("failureKind") or ""),
                "status_message": str(failure_summary.get("statusMessage") or ""),
                "test_failure_notice": test_failure_notice,
                "completed_keywords_today": completed_keywords_today,
                "detected_platforms_today": detected_platforms_today,
                "actual_screenshot_count_today": actual_screenshot_count_today,
                "fixed_screenshot_target_today": fixed_screenshot_target_today,
                "completed_by_quota_today": completed_by_quota_today,
                "platforms": all_platforms,
                "keywords": [
                    {
                        **kw,
                        "platforms": [self._platform_display_name(p) for p in (kw.get("platforms") or [])],
                        "deep_think": {
                            self._platform_display_name(k): v
                            for k, v in (kw.get("deep_think") or {}).items()
                        },
                    }
                    for kw in (task.get("keywords") or [])
                ],
                "webhook_url": self._secret_masker(task.get("webhook_url", "")),
                "weekdays": task.get("weekdays", [0, 1, 2, 3, 4]),
                "industry_tags": task.get("industry_tags", []),
                "region_tags": task.get("region_tags", []),
                "inspect": task.get("inspect", False),
                "recognition_enabled": task.get("recognition_enabled", False),
                "recognition_brands": task.get("recognition_brands", ""),
                "recognition_batch_size": max(1, int(task.get("recognition_batch_size", 3) or 3)),
                "extract_references_enabled": bool(task.get("extract_references_enabled", False)),
                "fixed_screenshot_enabled": bool(task.get("fixed_screenshot_enabled", False)),
                "fixed_screenshot_count": max(1, int(task.get("fixed_screenshot_count", task.get("recognition_batch_size", 3)) or 1)),
                "optimization_start_date": task.get("optimization_start_date", ""),
                "optimization_end_date": task.get("optimization_end_date", ""),
                "created_at": str(task.get("created_at") or ""),
                "delete_pending": bool(task.get("delete_pending")),
                "delete_pending_at": str(task.get("delete_pending_at") or ""),
                "delete_pending_expires_at": str(task.get("delete_pending_expires_at") or ""),
                "delete_pending_error": str(task.get("delete_pending_error") or ""),
                "cloud_task_id": task.get("cloud_task_id"),
                "cloud_task_key": str(task.get("cloud_task_key") or ""),
                "cloud_access_level": str(task.get("cloud_access_level") or ""),
                "cloud_config_version": task.get("cloud_config_version"),
                "cloud_assigned_operator_user_id": task.get("cloud_assigned_operator_user_id"),
                "cloud_assigned_operator_username": str(task.get("cloud_assigned_operator_username") or ""),
                "total_records": total_records,
                "success_records": success_records,
                "success_rate": round(success_records / total_records * 100, 1) if total_records > 0 else 0,
                "article_count": article_count,
                "optimization_trend": optimization_trend,
            })

        return {"tasks": result}


def _is_revoked_cloud_task(task: dict[str, Any]) -> bool:
    return str(task.get("cloud_access_level") or "").strip().lower() == "revoked"
