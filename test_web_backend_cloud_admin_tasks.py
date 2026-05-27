from __future__ import annotations

import copy
import threading
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from core.cloud_client import CloudClientError
from core.cloud_session_store import CloudSessionStore
from web_backend import AppRuntime


class FakeAdminTaskClient:
    def __init__(self, tasks: list[dict] | None = None) -> None:
        self.tasks = [copy.deepcopy(task) for task in (tasks or [])]
        self.created: list[dict] = []
        self.assigned: list[dict] = []

    def list_admin_tasks(self, _token: str) -> list[dict]:
        return [copy.deepcopy(task) for task in self.tasks]

    def create_admin_task(self, _token: str, payload: dict) -> dict:
        task = {
            "id": 100 + len(self.created),
            "workspace_id": 1,
            "task_key": payload["task_key"],
            "name": payload["name"],
            "brand": payload["brand"],
            "config_json": copy.deepcopy(payload.get("config_json") or {}),
            "config_version": 1,
            "enabled": bool(payload.get("enabled", True)),
        }
        self.created.append(copy.deepcopy(payload))
        self.tasks.append(copy.deepcopy(task))
        return copy.deepcopy(task)

    def assign_admin_task_member(
        self,
        _token: str,
        task_id: int,
        *,
        user_id: int,
        access_level: str,
        note: str,
    ) -> dict:
        event = {
            "task_id": task_id,
            "user_id": user_id,
            "access_level": access_level,
            "note": note,
        }
        self.assigned.append(event)
        for task in self.tasks:
            if int(task.get("id") or 0) == int(task_id):
                task["assigned_operator_user_id"] = user_id
                task["assigned_operator_username"] = f"user-{user_id}"
                break
        return event


class FakeAdminUserClient:
    def __init__(self) -> None:
        self.updated: list[dict] = []

    def update_admin_user(self, _token: str, user_id: int, payload: dict) -> dict:
        saved = copy.deepcopy(payload)
        self.updated.append({"user_id": user_id, "payload": saved})
        return {
            "id": user_id,
            "workspace_id": 1,
            "username": saved.get("username", "林见路"),
            "role": "viewer",
            "display_name": saved.get("display_name", saved.get("username", "林见路")),
            "view_all_tasks": bool(saved.get("view_all_tasks")),
            "visible_task_ids": copy.deepcopy(saved.get("visible_task_ids") or []),
            "enabled": True,
            "token_version": 1,
            "created_at": "2026-05-06T00:00:00Z",
        }


class FakeArticleClassificationClient:
    def __init__(self) -> None:
        self.resolved: list[dict] = []
        self.ignored: list[dict] = []
        self.jobs = [
            {
                "id": 11,
                "workspace_id": 1,
                "article_id": 21,
                "status": "unresolved",
                "reason_json": {"source": "article_upsert"},
                "created_at": "2026-05-06T00:00:00Z",
                "updated_at": "2026-05-06T00:00:00Z",
                "article": {
                    "id": 21,
                    "workspace_id": 1,
                    "canonical_url": "https://example.com/a",
                    "url_hash": "hash-a",
                    "title": "未归类文章",
                    "source": "媒体",
                    "media_type": "selfmedia",
                    "payload_json": {},
                    "created_at": "2026-05-06T00:00:00Z",
                    "updated_at": "2026-05-06T00:00:00Z",
                    "task_links": [],
                },
            }
        ]

    def list_admin_article_classification_jobs(self, _token: str, *, status: str, limit: int) -> list[dict]:
        self.last_list_args = {"status": status, "limit": limit}
        return copy.deepcopy(self.jobs)

    def resolve_admin_article_classification_job(
        self,
        _token: str,
        job_id: int,
        *,
        task_id: int,
        reason: str,
    ) -> dict:
        self.resolved.append({"job_id": job_id, "task_id": task_id, "reason": reason})
        job = copy.deepcopy(self.jobs[0])
        job["status"] = "resolved"
        job["resolved_task_id"] = task_id
        return job

    def ignore_admin_article_classification_job(self, _token: str, job_id: int, *, reason: str) -> dict:
        self.ignored.append({"job_id": job_id, "reason": reason})
        job = copy.deepcopy(self.jobs[0])
        job["status"] = "ignored"
        return job


class FakeCloudSessionStore:
    def load(self) -> dict:
        return {
            "base_url": "https://api.surfacedlab.com",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "user": {
                "id": 1,
                "workspace_id": 1,
                "username": "admin@example.com",
                "role": "admin",
            },
        }


class FakeLoginClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def login(self, *, username: str, password: str, device_id: str, app_version: str) -> dict:
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "token_type": "bearer",
            "user": {
                "id": 2,
                "workspace_id": 1,
                "username": username,
                "role": "operator",
            },
        }

    def verify_email(self, *, email: str, code: str, device_id: str, app_version: str) -> dict:
        return {
            "access_token": "verified-access-token",
            "refresh_token": "verified-refresh-token",
            "token_type": "bearer",
            "user": {
                "id": 3,
                "workspace_id": 1,
                "username": email,
                "role": "operator",
            },
        }


class Refresh401StatusClient:
    def __init__(self, base_url: str, timeout_seconds: float = 15.0) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def me(self, _access_token: str) -> dict:
        raise CloudClientError("Session revoked", status_code=401)

    def refresh(self, _refresh_token: str) -> dict:
        raise CloudClientError("Refresh token revoked", status_code=401)


class WebBackendCloudAdminTaskTests(unittest.TestCase):
    def _install_light_snapshot_runtime(self, runtime: AppRuntime, config_store: dict, base_snapshot: dict) -> None:
        holder = {"config": copy.deepcopy(config_store)}

        def load_config() -> dict:
            return copy.deepcopy(holder["config"])

        def save_config(config: dict) -> None:
            holder["config"] = copy.deepcopy(config)

        runtime._lock = threading.RLock()
        runtime.load_config = Mock(side_effect=load_config)  # type: ignore[method-assign]
        runtime.save_config = Mock(side_effect=save_config)  # type: ignore[method-assign]
        runtime._invalidate_tasks_full_cache = Mock()  # type: ignore[method-assign]
        runtime._invalidate_article_cache = Mock()  # type: ignore[method-assign]
        runtime._refresh_monitoring_runtime = Mock()  # type: ignore[method-assign]
        runtime.task_overview_service = Mock()
        runtime.task_overview_service.get_cached_task_snapshot.return_value = copy.deepcopy(base_snapshot)
        runtime.task_overview_service.get_tasks_full.side_effect = AssertionError("full snapshot should not be rebuilt")

    def test_update_task_returns_light_snapshot_without_rebuilding_tasks_full(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        self._install_light_snapshot_runtime(
            runtime,
            {
                "detection_mode": "browser",
                "scheduler": {"weekly_times": {"0": "09:30"}},
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "旧品牌",
                        "brand": "旧品牌",
                        "enabled": True,
                        "webhook_url": "https://example.com/hook-secret",
                        "weekdays": [0],
                        "keywords": [{"keyword": "旧词", "brand": "旧品牌", "platforms": ["doubao"], "mode": "browser"}],
                    }
                ],
            },
            {
                "id": "task-1",
                "total_records": 12,
                "success_records": 9,
                "success_rate": 75.0,
                "article_count": 7,
                "optimization_trend": [{"value": 80.0}],
            },
        )

        with patch("web_backend.local_today", return_value=date(2026, 1, 5)):
            with patch(
                "web_backend.get_task_day_status",
                return_value={"status": "pending", "brand_status": "pending", "gap_reasons": []},
            ):
                with patch(
                    "web_backend._build_task_failure_summary_impl",
                    return_value={
                        "failedToday": False,
                        "failedModes": [],
                        "failedUpdatedAt": "",
                        "failureKind": "",
                        "statusMessage": "",
                    },
                ):
                    with patch(
                        "web_backend._collect_today_successful_task_payload_impl",
                        return_value={
                            "completedKeywords": ["新词"],
                            "detectedPlatforms": ["doubao"],
                            "actualScreenshotCount": 1,
                        },
                    ):
                        result = runtime.update_task(
                            "task-1",
                            {
                                "name": "新品牌",
                                "brand": "新品牌",
                                "weekdays": [0],
                                "keywords": [{"keyword": "新词", "brand": "新品牌", "platforms": ["doubao"], "mode": "browser"}],
                            },
                        )

        self.assertTrue(result["ok"])
        task = result["task"]
        self.assertEqual(task["id"], "task-1")
        self.assertEqual(task["name"], "新品牌")
        self.assertEqual(task["article_count"], 7)
        self.assertEqual(task["total_records"], 12)
        self.assertEqual(task["completed_keywords_today"], ["新词"])
        runtime.task_overview_service.get_tasks_full.assert_not_called()

    def test_update_task_blank_webhook_preserves_existing_secret(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        holder = {
            "config": {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "旧品牌",
                        "brand": "旧品牌",
                        "enabled": True,
                        "webhook_url": "https://example.com/hook-secret",
                        "keywords": [{"keyword": "旧词", "brand": "旧品牌", "platforms": ["doubao"], "mode": "browser"}],
                    }
                ]
            }
        }

        runtime._lock = threading.RLock()
        runtime.load_config = Mock(side_effect=lambda: copy.deepcopy(holder["config"]))  # type: ignore[method-assign]
        runtime.save_config = Mock(side_effect=lambda config: holder.update(config=copy.deepcopy(config)))  # type: ignore[method-assign]
        runtime._invalidate_tasks_full_cache = Mock()  # type: ignore[method-assign]
        runtime._invalidate_article_cache = Mock()  # type: ignore[method-assign]
        runtime._refresh_monitoring_runtime = Mock()  # type: ignore[method-assign]
        runtime._get_cached_task_snapshot = Mock(return_value={})  # type: ignore[method-assign]
        runtime._get_light_task_snapshot = Mock(return_value={"id": "task-1"})  # type: ignore[method-assign]

        result = runtime.update_task(
            "task-1",
            {
                "name": "新品牌",
                "brand": "新品牌",
                "webhook_url": "",
                "keywords": [{"keyword": "新词", "brand": "新品牌", "platforms": ["doubao"], "mode": "browser"}],
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(holder["config"]["tasks"][0]["webhook_url"], "https://example.com/hook-secret")
        self.assertEqual(holder["config"]["tasks"][0]["name"], "新品牌")

    def test_sync_cloud_admin_task_returns_light_local_snapshot_without_rebuilding_tasks_full(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        self._install_light_snapshot_runtime(
            runtime,
            {
                "detection_mode": "browser",
                "scheduler": {"weekly_times": {"0": "09:30"}},
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "品牌A",
                        "brand": "品牌A",
                        "enabled": True,
                        "weekdays": [0],
                        "keywords": [{"keyword": "品牌A", "brand": "品牌A", "platforms": ["doubao"], "mode": "browser"}],
                    }
                ],
            },
            {
                "id": "task-1",
                "total_records": 5,
                "success_records": 4,
                "success_rate": 80.0,
                "article_count": 3,
                "optimization_trend": [{"value": 88.0}],
            },
        )
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": {
                "id": 100,
                "task_key": "local-task-1",
                "workspace_id": 1,
                "config_version": 1,
                "assigned_operator_username": "user-7",
            },
        })

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            with patch("web_backend.local_today", return_value=date(2026, 1, 5)):
                with patch(
                    "web_backend.get_task_day_status",
                    return_value={"status": "pending", "brand_status": "pending", "gap_reasons": []},
                ):
                    with patch(
                        "web_backend._build_task_failure_summary_impl",
                        return_value={
                            "failedToday": False,
                            "failedModes": [],
                            "failedUpdatedAt": "",
                            "failureKind": "",
                            "statusMessage": "",
                        },
                    ):
                        with patch(
                            "web_backend._collect_today_successful_task_payload_impl",
                            return_value={"completedKeywords": [], "detectedPlatforms": [], "actualScreenshotCount": 0},
                        ):
                            result = runtime.sync_cloud_admin_task({"local_task_id": "task-1", "operator_user_id": 7})

        self.assertTrue(result["ok"])
        self.assertEqual(result["local_task"]["cloud_task_id"], 100)
        self.assertEqual(result["local_task"]["article_count"], 3)
        runtime._cloud_runtime_command.assert_called_once()  # type: ignore[attr-defined]
        command_name, payload = runtime._cloud_runtime_command.call_args.args  # type: ignore[attr-defined]
        self.assertEqual(command_name, "cloud.sync_admin_task")
        self.assertEqual(payload["operator_user_id"], 7)
        runtime.task_overview_service.get_tasks_full.assert_not_called()

    def test_sync_cloud_admin_task_defaults_unassigned_operator_to_admin(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        self._install_light_snapshot_runtime(
            runtime,
            {
                "detection_mode": "browser",
                "scheduler": {"weekly_times": {"0": "09:30"}},
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "品牌A",
                        "brand": "品牌A",
                        "enabled": True,
                        "weekdays": [0],
                        "keywords": [{"keyword": "品牌A", "brand": "品牌A", "platforms": ["doubao"], "mode": "browser"}],
                    }
                ],
            },
            {
                "id": "task-1",
                "total_records": 5,
                "success_records": 4,
                "success_rate": 80.0,
                "article_count": 3,
                "optimization_trend": [{"value": 88.0}],
            },
        )
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": {
                "id": 100,
                "task_key": "local-task-1",
                "workspace_id": 1,
                "config_version": 1,
                "assigned_operator_username": "user-1",
            },
        })

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            with patch("web_backend.local_today", return_value=date(2026, 1, 5)):
                with patch(
                    "web_backend.get_task_day_status",
                    return_value={"status": "pending", "brand_status": "pending", "gap_reasons": []},
                ):
                    with patch(
                        "web_backend._build_task_failure_summary_impl",
                        return_value={
                            "failedToday": False,
                            "failedModes": [],
                            "failedUpdatedAt": "",
                            "failureKind": "",
                            "statusMessage": "",
                        },
                    ):
                        with patch(
                            "web_backend._collect_today_successful_task_payload_impl",
                            return_value={"completedKeywords": [], "detectedPlatforms": [], "actualScreenshotCount": 0},
                        ):
                            result = runtime.sync_cloud_admin_task({"local_task_id": "task-1"})

        self.assertTrue(result["ok"])
        command_name, payload = runtime._cloud_runtime_command.call_args.args  # type: ignore[attr-defined]
        self.assertEqual(command_name, "cloud.sync_admin_task")
        self.assertEqual(payload["operator_user_id"], 1)
        self.assertEqual(result["local_task"]["cloud_assigned_operator_user_id"], 1)

    def test_admin_task_list_ensures_unsynced_local_tasks_exist_in_cloud(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        client = FakeAdminTaskClient([
            {
                "id": 1,
                "workspace_id": 1,
                "task_key": "existing-key",
                "name": "已有品牌",
                "brand": "已有品牌",
                "config_json": {},
                "config_version": 2,
                "enabled": True,
            }
        ])

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            payload = runtime._ensure_local_admin_tasks_in_cloud(
                client,
                "token",
                [
                    {
                        "local_task_id": "local-existing",
                        "cloud_task_id": 0,
                        "task_key": "existing-key",
                        "payload": {"name": "已有品牌", "brand": "已有品牌", "config_json": {}, "enabled": True},
                        "operator_user_id": 0,
                    },
                    {
                        "local_task_id": "local-new",
                        "cloud_task_id": 0,
                        "task_key": "new-key",
                        "payload": {"name": "新品牌", "brand": "新品牌", "config_json": {}, "enabled": True},
                        "operator_user_id": 7,
                    },
                ],
            )

        self.assertEqual([item["task_key"] for item in payload["tasks"]], ["existing-key", "new-key"])
        self.assertEqual([item["task_key"] for item in client.created], ["new-key"])
        self.assertEqual([item["user_id"] for item in client.assigned], [1, 7])
        updates_by_id = {item["local_task_id"]: item["task"]["id"] for item in payload["local_updates"]}
        self.assertEqual(updates_by_id["local-existing"], 1)
        self.assertEqual(updates_by_id["local-new"], 100)

    def test_list_cloud_admin_tasks_routes_runtime_command_boundary_and_applies_local_updates(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_task_ensure_snapshots = Mock(return_value=[{"local_task_id": "task-1"}])  # type: ignore[method-assign]
        runtime._apply_cloud_task_local_updates = Mock()  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": {
                "tasks": [{"id": 1, "task_key": "task-1"}],
                "local_updates": [{"local_task_id": "task-1", "task": {"id": 1}}],
            },
        })

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.list_cloud_admin_tasks()

        self.assertTrue(result["ok"])
        self.assertEqual(result["tasks"][0]["id"], 1)
        runtime._cloud_runtime_command.assert_called_once_with(  # type: ignore[attr-defined]
            "cloud.list_admin_tasks_with_local_sync",
            {"local_snapshots": [{"local_task_id": "task-1"}]},
        )
        runtime._apply_cloud_task_local_updates.assert_called_once_with(  # type: ignore[attr-defined]
            [{"local_task_id": "task-1", "task": {"id": 1}}]
        )

    def test_update_cloud_admin_user_preserves_custom_viewer_task_ids(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": {
                "id": 2,
                "workspace_id": 1,
                "username": "林见路",
                "role": "viewer",
                "display_name": "林见路",
                "view_all_tasks": False,
                "visible_task_ids": [8, 9],
                "enabled": True,
                "token_version": 1,
                "created_at": "2026-05-06T00:00:00Z",
            },
        })

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.update_cloud_admin_user({
                "user_id": 2,
                "username": "林见路",
                "display_name": "林见路",
                "view_all_tasks": False,
                "visible_task_ids": [8, 9],
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["user"]["visible_task_ids"], [8, 9])
        runtime._cloud_runtime_command.assert_called_once_with(  # type: ignore[attr-defined]
            "cloud.update_admin_user",
            {
                "user_id": 2,
                "payload": {
                    "username": "林见路",
                    "display_name": "林见路",
                    "view_all_tasks": False,
                    "visible_task_ids": [8, 9],
                },
            },
        )

    def test_list_cloud_admin_users_routes_runtime_command_boundary(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": [{"id": 2, "username": "operator-2"}],
        })

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.list_cloud_admin_users()

        self.assertTrue(result["ok"])
        self.assertEqual(result["users"][0]["username"], "operator-2")
        runtime._cloud_runtime_command.assert_called_once_with("cloud.list_admin_users", None)  # type: ignore[attr-defined]

    def test_list_cloud_article_classification_jobs_forwards_admin_request(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": [
                {
                    "id": 11,
                    "article": {"title": "未归类文章"},
                }
            ],
        })

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.list_cloud_article_classification_jobs()

        self.assertTrue(result["ok"])
        self.assertEqual(result["jobs"][0]["article"]["title"], "未归类文章")
        runtime._cloud_runtime_command.assert_called_once_with(  # type: ignore[attr-defined]
            "cloud.list_admin_article_classification_jobs",
            {"status": "unresolved", "limit": 200},
        )

    def test_resolve_cloud_article_classification_job_refreshes_articles(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": {"id": 11, "task_id": 3},
        })
        runtime._refresh_cloud_articles_after_classification_change = Mock(  # type: ignore[method-assign]
            return_value={"ok": True, "imported": 1}
        )

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.resolve_cloud_article_classification_job({"job_id": 11, "task_id": 3})

        self.assertTrue(result["ok"])
        runtime._cloud_runtime_command.assert_called_once_with(  # type: ignore[attr-defined]
            "cloud.resolve_admin_article_classification_job",
            {"job_id": 11, "task_id": 3, "reason": "管理员归类未归类文章"},
        )
        runtime._refresh_cloud_articles_after_classification_change.assert_called_once()

    def test_create_cloud_admin_user_routes_runtime_command_boundary(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": {"id": 9, "visible_task_ids": [8, 9]},
        })

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.create_cloud_admin_user({
                "username": "viewer9",
                "password": "Password123",
                "role": "viewer",
                "view_all_tasks": False,
                "visible_task_ids": [8, 9],
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["user"]["visible_task_ids"], [8, 9])
        runtime._cloud_runtime_command.assert_called_once_with(  # type: ignore[attr-defined]
            "cloud.create_admin_user",
            {
                "payload": {
                    "username": "viewer9",
                    "password": "Password123",
                    "role": "viewer",
                    "display_name": None,
                    "email": None,
                    "birthday": None,
                    "hire_date": None,
                    "view_all_tasks": False,
                    "visible_task_ids": [8, 9],
                },
            },
        )

    def test_update_cloud_admin_task_routes_runtime_command_boundary(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": {"id": 42, "name": "Brand A"},
        })

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.update_cloud_admin_task({
                "task_id": 42,
                "name": "Brand A",
                "enabled": True,
                "expected_config_version": 7,
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["id"], 42)
        runtime._cloud_runtime_command.assert_called_once_with(  # type: ignore[attr-defined]
            "cloud.update_admin_task",
            {
                "task_id": 42,
                "payload": {
                    "name": "Brand A",
                    "enabled": True,
                    "expected_config_version": 7,
                },
            },
        )

    def test_operator_article_visibility_keeps_only_current_visible_brands(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        config = {
            "tasks": [
                {
                    "task_id": "local-jisou",
                    "name": "即搜AI",
                    "brand": "即搜AI",
                    "cloud_task_id": 42,
                    "cloud_access_level": "operate",
                }
            ]
        }
        articles = [
            {
                "title": "云端老文章",
                "cloud_task_ids": [42],
                "matched_tasks": ["别的品牌"],
                "match_reasons": {"别的品牌": ["历史归类"]},
            },
            {
                "title": "本地新文章",
                "matched_tasks": ["即搜AI"],
                "match_reasons": {"即搜AI": ["标题匹配"]},
            },
            {
                "title": "其他品牌残留",
                "matched_tasks": ["别的品牌"],
                "match_reasons": {"别的品牌": ["标题匹配"]},
            },
            {
                "title": "普通账号未归类残留",
                "matched_tasks": [],
                "unmatched_reason": "未命中当前品牌",
            },
        ]

        result = runtime._filter_articles_for_current_cloud_visibility(
            articles,
            config,
            session={
                "access_token": "token",
                "user": {"id": 2, "workspace_id": 1, "role": "operator"},
            },
        )

        self.assertEqual([item["title"] for item in result], ["云端老文章", "本地新文章"])
        self.assertEqual(result[0]["matched_tasks"], ["即搜AI"])
        self.assertEqual(result[1]["matched_tasks"], ["即搜AI"])

    def test_admin_article_visibility_keeps_all_local_articles(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        articles = [
            {"title": "已归类", "matched_tasks": ["即搜AI"]},
            {"title": "未归类", "matched_tasks": []},
        ]

        result = runtime._filter_articles_for_current_cloud_visibility(
            articles,
            {"tasks": []},
            session={
                "access_token": "token",
                "user": {"id": 1, "workspace_id": 1, "role": "admin"},
            },
        )

        self.assertEqual([item["title"] for item in result], ["已归类", "未归类"])

    def test_cloud_login_returns_success_without_blocking_incremental_pull(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._lock = threading.RLock()
        runtime._cloud_status_validation_lock = threading.RLock()
        runtime._cloud_status_validation_error = ""  # type: ignore[attr-defined]
        runtime._cloud_platform_auto_sync = Mock()
        runtime._cloud_platform_auto_sync.get_status.return_value = {}
        runtime._login_cloud_account_space = Mock(return_value={  # type: ignore[method-assign]
            "base_url": "https://api.surfacedlab.com",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "saved_at": "2026-05-15T10:00:00",
            "user": {"id": 2, "workspace_id": 1, "role": "operator"},
        })
        runtime.pull_cloud_tasks = Mock(return_value={"ok": True, "message": "云端任务无变化"})  # type: ignore[method-assign]

        with patch("web_backend.SurfacedCloudClient", FakeLoginClient):
            result = runtime.login_cloud({
                "base_url": "https://api.surfacedlab.com",
                "username": "operator001",
                "password": "Operator123456",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "云端登录成功")
        self.assertTrue(result["cloud"]["loggedIn"])
        runtime.pull_cloud_tasks.assert_not_called()

    def test_cloud_status_validation_keeps_session_on_passive_refresh_401(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save(
                {
                    "base_url": "https://api.surfacedlab.com",
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                }
            )
            runtime = AppRuntime.__new__(AppRuntime)
            runtime._cloud_status_validation_lock = threading.RLock()
            runtime._cloud_status_validated_identity = ""
            runtime._cloud_status_validated_at = 0.0
            runtime._cloud_status_validation_error = ""

            with (
                patch("web_backend.CloudSessionStore", return_value=store),
                patch("web_backend.SurfacedCloudClient", Refresh401StatusClient),
            ):
                runtime._validate_cloud_session_if_needed(force=True)  # noqa: SLF001

            self.assertEqual(store.load()["access_token"], "old-access")
            self.assertEqual(runtime._cloud_status_validation_error, "Refresh token revoked")

    def test_cloud_login_returns_immediately_after_auth_without_blocking_pull(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._lock = threading.RLock()
        runtime._cloud_status_validation_lock = threading.RLock()
        runtime._cloud_status_validation_error = ""  # type: ignore[attr-defined]
        runtime._cloud_platform_auto_sync = Mock()
        runtime._cloud_platform_auto_sync.get_status.return_value = {}
        runtime._login_cloud_account_space = Mock(return_value={
            "base_url": "https://api.surfacedlab.com",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "saved_at": "2026-05-15T10:00:00",
            "user": {"id": 2, "workspace_id": 1, "username": "operator001", "role": "operator"},
        })  # type: ignore[method-assign]
        runtime.pull_cloud_tasks = Mock(return_value={"ok": True, "message": "云端任务无变化"})  # type: ignore[method-assign]

        with patch("web_backend.SurfacedCloudClient", FakeLoginClient):
            result = runtime.login_cloud({
                "base_url": "https://api.surfacedlab.com",
                "username": "operator001",
                "password": "Operator123456",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "云端登录成功")
        self.assertTrue(result["cloud"]["loggedIn"])
        runtime.pull_cloud_tasks.assert_not_called()

    def test_verify_cloud_email_returns_immediately_after_auth_without_blocking_pull(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._lock = threading.RLock()
        runtime._cloud_status_validation_lock = threading.RLock()
        runtime._cloud_status_validation_error = ""  # type: ignore[attr-defined]
        runtime._cloud_platform_auto_sync = Mock()
        runtime._cloud_platform_auto_sync.get_status.return_value = {}
        runtime._login_cloud_account_space = Mock(return_value={
            "base_url": "https://api.surfacedlab.com",
            "access_token": "verified-access-token",
            "refresh_token": "verified-refresh-token",
            "saved_at": "2026-05-15T10:00:00",
            "user": {"id": 3, "workspace_id": 1, "username": "admin@example.com", "role": "operator"},
        })  # type: ignore[method-assign]
        runtime.pull_cloud_tasks = Mock(return_value={"ok": True, "message": "云端任务无变化"})  # type: ignore[method-assign]

        with patch("web_backend.SurfacedCloudClient", FakeLoginClient):
            result = runtime.verify_cloud_email({
                "base_url": "https://api.surfacedlab.com",
                "email": "admin@example.com",
                "code": "123456",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "邮箱验证成功")
        self.assertTrue(result["cloud"]["loggedIn"])
        runtime.pull_cloud_tasks.assert_not_called()

    def test_cloud_endpoint_wrappers_route_through_runtime_command_boundary(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_runtime_command = Mock(side_effect=[
            {"ok": True, "cloud": {"loggedIn": True}},
            {"ok": True, "cloud": {"loggedIn": True, "savedAt": "now"}},
            {"ok": True, "cloud": {"loggedIn": False}},
            {"ok": True, "message": "已退出云端"},
            {"ok": True, "metrics": {"event_count": 2}},
            {"ok": True, "run_records": {"queued": 1}},
        ])  # type: ignore[method-assign]

        self.assertEqual(runtime.get_cloud_status(), {"ok": True, "cloud": {"loggedIn": True}})
        self.assertEqual(runtime._current_cloud_status(), {"ok": True, "cloud": {"loggedIn": True, "savedAt": "now"}})  # noqa: SLF001
        self.assertEqual(runtime._cloud_status_from_session({"access_token": "token"}), {"ok": True, "cloud": {"loggedIn": False}})  # noqa: SLF001
        self.assertEqual(runtime.logout_cloud({}), {"ok": True, "message": "已退出云端"})
        self.assertEqual(runtime.flush_cloud_outbox({"limit": 7}), {"ok": True, "metrics": {"event_count": 2}})
        self.assertEqual(runtime._recover_cloud_run_history_uploads(), {"ok": True, "run_records": {"queued": 1}})  # noqa: SLF001

        self.assertEqual(
            runtime._cloud_runtime_command.call_args_list,
            [
                unittest.mock.call("cloud.status"),
                unittest.mock.call("cloud.current_status"),
                unittest.mock.call("cloud.status_from_session", {"session": {"access_token": "token"}}),
                unittest.mock.call("cloud.logout"),
                unittest.mock.call("cloud.flush_outbox", {"limit": 7}),
                unittest.mock.call("cloud.recover_uploads"),
            ],
        )

    def test_login_account_space_wrapper_routes_through_runtime_command_boundary(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_runtime_command = Mock(return_value={
            "ok": True,
            "session": {"base_url": "https://api.surfacedlab.com", "access_token": "access"},
        })  # type: ignore[method-assign]

        result = runtime._login_cloud_account_space(  # noqa: SLF001
            base_url="https://api.surfacedlab.com",
            token_pair={"access_token": "access", "refresh_token": "refresh"},
        )

        self.assertEqual(result, {"base_url": "https://api.surfacedlab.com", "access_token": "access"})
        runtime._cloud_runtime_command.assert_called_once_with(
            "cloud.login_account_space",
            {
                "base_url": "https://api.surfacedlab.com",
                "token_pair": {"access_token": "access", "refresh_token": "refresh"},
            },
        )

    def test_delete_cloud_task_flushes_outbox_through_runtime_support(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._current_cloud_role = Mock(return_value="admin")  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(side_effect=[  # type: ignore[method-assign]
            {"ok": True},
            {"ok": True, "payload": {}},
        ])

        ok, message = runtime._delete_cloud_task_for_local_task({"cloud_task_id": 42})  # noqa: SLF001

        self.assertTrue(ok)
        self.assertEqual(message, "")
        self.assertEqual(  # type: ignore[attr-defined]
            runtime._cloud_runtime_command.call_args_list,
            [
                unittest.mock.call("cloud.flush_outbox", {"limit": 10000}),
                unittest.mock.call("cloud.delete_admin_task", {"task_id": 42}),
            ],
        )

    def test_delete_cloud_task_continues_when_predelete_flush_fails(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._current_cloud_role = Mock(return_value="admin")  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(side_effect=[  # type: ignore[method-assign]
            RuntimeError("flush failed"),
            {"ok": True, "payload": {}},
        ])

        ok, message = runtime._delete_cloud_task_for_local_task({"cloud_task_id": 42})  # noqa: SLF001

        self.assertTrue(ok)
        self.assertEqual(message, "")
        self.assertEqual(runtime._cloud_runtime_command.call_count, 2)  # type: ignore[attr-defined]

    def test_restore_cloud_deleted_task_routes_runtime_command_boundary(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._current_cloud_role = Mock(return_value="admin")  # type: ignore[method-assign]
        runtime._cloud_runtime_command = Mock(return_value={  # type: ignore[method-assign]
            "ok": True,
            "payload": {"id": 42, "name": "Brand A"},
        })

        ok, message, task = runtime._restore_cloud_deleted_task({"cloud_task_id": 42})  # noqa: SLF001

        self.assertTrue(ok)
        self.assertEqual(message, "")
        self.assertEqual(task["id"], 42)
        runtime._cloud_runtime_command.assert_called_once_with(  # type: ignore[attr-defined]
            "cloud.restore_admin_task",
            {"task_id": 42},
        )

    def test_operator_article_upload_snapshot_keeps_unmatched_candidates(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        config = {
            "tasks": [
                {
                    "task_id": "local-jisou",
                    "name": "即搜AI",
                    "brand": "即搜AI",
                    "cloud_task_id": 42,
                    "cloud_access_level": "operate",
                }
            ]
        }
        articles = [
            {
                "title": "可见品牌文章",
                "matched_tasks": ["即搜AI"],
                "match_reasons": {"即搜AI": ["标题匹配"]},
                "cloud_task_ids": [42],
            },
            {
                "title": "本地未归类候选",
                "matched_tasks": [],
                "match_reasons": {},
                "unmatched_reason": "未命中当前品牌",
            },
            {
                "title": "其他品牌残留候选",
                "matched_tasks": ["别的品牌"],
                "match_reasons": {"别的品牌": ["历史归类"]},
                "cloud_task_ids": [99],
            },
        ]

        with (
            patch(
                "web_backend.schedule_article_match_refresh",
                return_value={"scheduled": False, "reason": "nothing_to_refresh"},
            ),
            patch("web_backend.refresh_article_matches", return_value=copy.deepcopy(articles)),
        ):
            result = runtime._get_cloud_article_upload_snapshot(
                config,
                session={
                    "access_token": "token",
                    "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                },
            )

        self.assertEqual([item["title"] for item in result], ["可见品牌文章", "本地未归类候选", "其他品牌残留候选"])
        self.assertEqual(result[0]["matched_tasks"], ["即搜AI"])
        self.assertEqual(result[1]["matched_tasks"], [])
        self.assertEqual(result[2]["matched_tasks"], [])
        self.assertEqual(result[2]["match_reasons"], {})
        self.assertEqual(result[2]["cloud_task_ids"], [])


if __name__ == "__main__":
    unittest.main()
