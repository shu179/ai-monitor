from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock, patch

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


class WebBackendCloudAdminTaskTests(unittest.TestCase):
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
        self.assertEqual(client.assigned[0]["user_id"], 7)
        updates_by_id = {item["local_task_id"]: item["task"]["id"] for item in payload["local_updates"]}
        self.assertEqual(updates_by_id["local-existing"], 1)
        self.assertEqual(updates_by_id["local-new"], 100)

    def test_update_cloud_admin_user_preserves_custom_viewer_task_ids(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        client = FakeAdminUserClient()
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_request_with_refresh = Mock(  # type: ignore[method-assign]
            side_effect=lambda operation: (True, operation(client, "access-token"), "")
        )

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.update_cloud_admin_user({
                "user_id": 2,
                "username": "林见路",
                "display_name": "林见路",
                "view_all_tasks": False,
                "visible_task_ids": [8, 9],
            })

        self.assertTrue(result["ok"])
        self.assertEqual(client.updated[0]["payload"]["visible_task_ids"], [8, 9])
        self.assertEqual(result["user"]["visible_task_ids"], [8, 9])

    def test_list_cloud_article_classification_jobs_forwards_admin_request(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        client = FakeArticleClassificationClient()
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_request_with_refresh = Mock(  # type: ignore[method-assign]
            side_effect=lambda operation: (True, operation(client, "access-token"), "")
        )

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.list_cloud_article_classification_jobs()

        self.assertTrue(result["ok"])
        self.assertEqual(result["jobs"][0]["article"]["title"], "未归类文章")
        self.assertEqual(client.last_list_args, {"status": "unresolved", "limit": 200})

    def test_resolve_cloud_article_classification_job_refreshes_articles(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        client = FakeArticleClassificationClient()
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._cloud_request_with_refresh = Mock(  # type: ignore[method-assign]
            side_effect=lambda operation: (True, operation(client, "access-token"), "")
        )
        runtime._refresh_cloud_articles_after_classification_change = Mock(  # type: ignore[method-assign]
            return_value={"ok": True, "imported": 1}
        )

        with patch("web_backend.CloudSessionStore", return_value=FakeCloudSessionStore()):
            result = runtime.resolve_cloud_article_classification_job({"job_id": 11, "task_id": 3})

        self.assertTrue(result["ok"])
        self.assertEqual(client.resolved[0]["job_id"], 11)
        self.assertEqual(client.resolved[0]["task_id"], 3)
        runtime._refresh_cloud_articles_after_classification_change.assert_called_once()

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

    def test_cloud_login_uses_incremental_pull_after_auth(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.get_cloud_status = Mock(return_value={"cloud": {"loggedIn": True}})  # type: ignore[method-assign]
        runtime._login_cloud_account_space = Mock(return_value={  # type: ignore[method-assign]
            "base_url": "https://api.surfacedlab.com",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
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
        runtime.pull_cloud_tasks.assert_called_once_with({"force": False})

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

        with patch("web_backend.refresh_article_matches", return_value=copy.deepcopy(articles)):
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
