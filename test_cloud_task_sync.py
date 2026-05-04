from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import core.history as history_module
from core.cloud_client import CloudClientError
from core.cloud_session_store import CloudSessionStore
from core.cloud_task_sync import merge_cloud_tasks_into_config, pull_cloud_tasks_into_config


class FakeTaskClient:
    def __init__(
        self,
        tasks: list[dict],
        *,
        deleted_tasks: list[dict] | None = None,
        fail_once_401: bool = False,
        fail_run_records_once_401: bool = False,
        run_records_by_task: dict[int, list[dict]] | None = None,
    ) -> None:
        self.tasks = tasks
        self.deleted_tasks = deleted_tasks or []
        self.fail_once_401 = fail_once_401
        self.fail_run_records_once_401 = fail_run_records_once_401
        self.run_records_by_task = run_records_by_task or {}
        self.list_tokens: list[str] = []
        self.list_deleted_tokens: list[str] = []
        self.run_record_calls: list[tuple[str, int, int]] = []
        self.refresh_calls = 0

    def list_tasks(self, access_token: str) -> list[dict]:
        self.list_tokens.append(access_token)
        if self.fail_once_401:
            self.fail_once_401 = False
            raise CloudClientError("expired", status_code=401)
        return self.tasks

    def list_deleted_tasks(self, access_token: str) -> list[dict]:
        self.list_deleted_tokens.append(access_token)
        return self.deleted_tasks

    def refresh(self, refresh_token: str) -> dict:
        self.refresh_calls += 1
        return {
            "access_token": "new-access",
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {"id": 2, "workspace_id": 1, "role": "operator"},
        }

    def task_run_records(
        self,
        access_token: str,
        task_id: int,
        *,
        limit: int = 50,
        since_id: int | None = None,
    ) -> list[dict]:
        normalized_since_id = int(since_id or 0)
        self.run_record_calls.append((access_token, task_id, limit, normalized_since_id))
        if self.fail_run_records_once_401:
            self.fail_run_records_once_401 = False
            raise CloudClientError("expired run records token", status_code=401)
        records = [
            record for record in self.run_records_by_task.get(task_id, [])
            if int(record.get("id") or 0) > normalized_since_id
        ]
        return list(records)[:limit]


class CloudTaskSyncTests(unittest.TestCase):
    def test_merge_adds_cloud_task_and_normalizes_keyword_shape(self):
        config = {"tasks": []}
        summary = merge_cloud_tasks_into_config(
            config,
            [
                {
                    "id": 1,
                    "workspace_id": 1,
                    "task_key": "brand-demo",
                    "name": "测试品牌任务",
                    "brand": "测试品牌",
                    "config_json": {"keywords": ["测试关键词"], "platforms": ["Kimi", "DeepSeek"]},
                    "config_version": 3,
                    "enabled": True,
                    "access_level": "operate",
                }
            ],
            cloud_user={"workspace_id": 1, "role": "operator"},
            base_url="https://api.example.com",
        )

        self.assertEqual(summary["added"], 1)
        task = config["tasks"][0]
        self.assertEqual(task["task_id"], "cloud_1")
        self.assertEqual(task["cloud_task_id"], 1)
        self.assertEqual(task["cloud_task_key"], "brand-demo")
        self.assertEqual(task["cloud_access_level"], "operate")
        self.assertTrue(task["enabled"])
        self.assertEqual(task["keywords"][0]["keyword"], "测试关键词")
        self.assertEqual(task["keywords"][0]["platforms"], ["kimi", "deepseek"])
        self.assertEqual(task["keywords"][0]["mode"], "browser")

    def test_merge_matches_unique_legacy_task_by_name_and_preserves_local_fields(self):
        config = {
            "tasks": [
                {
                    "task_id": "legacy_keep",
                    "name": "测试品牌任务",
                    "brand": "测试品牌",
                    "webhook_url": "https://example.com/hook",
                    "weekdays": [0, 1, 2],
                    "keywords": [{"keyword": "旧词", "brand": "测试品牌", "platforms": ["doubao"], "mode": "browser"}],
                }
            ]
        }

        summary = merge_cloud_tasks_into_config(
            config,
            [
                {
                    "id": 7,
                    "workspace_id": 1,
                    "task_key": "brand-demo-7",
                    "name": "测试品牌任务",
                    "brand": "测试品牌",
                    "config_json": {"keywords": [{"keyword": "新词", "platforms": ["Kimi"]}]},
                    "config_version": 1,
                    "enabled": True,
                    "access_level": "operate",
                }
            ],
            cloud_user={"workspace_id": 1, "role": "operator"},
        )

        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["matched_by"]["name_brand"], 1)
        self.assertEqual(len(config["tasks"]), 1)
        task = config["tasks"][0]
        self.assertEqual(task["task_id"], "legacy_keep")
        self.assertEqual(task["cloud_task_id"], 7)
        self.assertEqual(task["webhook_url"], "https://example.com/hook")
        self.assertEqual(task["weekdays"], [0, 1, 2])
        self.assertEqual(task["keywords"][0]["keyword"], "新词")

    def test_viewer_tasks_are_display_only_and_missing_cloud_tasks_are_revoked(self):
        config = {
            "tasks": [
                {
                    "task_id": "cloud_5",
                    "name": "旧云端任务",
                    "brand": "旧品牌",
                    "enabled": True,
                    "cloud_task_id": 5,
                    "cloud_workspace_id": 1,
                    "cloud_base_url": "https://api.example.com",
                    "cloud_access_level": "operate",
                }
            ]
        }

        summary = merge_cloud_tasks_into_config(
            config,
            [
                {
                    "id": 6,
                    "workspace_id": 1,
                    "task_key": "viewer-task",
                    "name": "浏览任务",
                    "brand": "浏览品牌",
                    "config_json": {"keywords": ["浏览品牌"], "platforms": ["doubao"]},
                    "config_version": 1,
                    "enabled": True,
                    "access_level": "view",
                }
            ],
            cloud_user={"workspace_id": 1, "role": "viewer"},
            base_url="https://api.example.com",
        )

        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["revoked"], 1)
        revoked = next(task for task in config["tasks"] if task.get("cloud_task_id") == 5)
        viewer = next(task for task in config["tasks"] if task.get("cloud_task_id") == 6)
        self.assertFalse(revoked["enabled"])
        self.assertEqual(revoked["cloud_access_level"], "revoked")
        self.assertFalse(viewer["enabled"])
        self.assertEqual(viewer["cloud_access_level"], "view")

    def test_deleted_cloud_task_moves_local_task_to_recycle_bin(self):
        config = {
            "tasks": [
                {
                    "task_id": "cloud_5",
                    "name": "旧云端任务",
                    "brand": "旧品牌",
                    "enabled": True,
                    "cloud_task_id": 5,
                    "cloud_workspace_id": 1,
                    "cloud_base_url": "https://api.example.com",
                    "cloud_access_level": "operate",
                }
            ]
        }

        summary = merge_cloud_tasks_into_config(
            config,
            [],
            deleted_cloud_tasks=[
                {
                    "id": 5,
                    "workspace_id": 1,
                    "task_key": "old-task",
                    "name": "旧云端任务",
                    "brand": "旧品牌",
                    "config_json": {"keywords": ["旧品牌"], "platforms": ["doubao"]},
                    "config_version": 2,
                    "enabled": True,
                    "access_level": "operate",
                    "deleted_at": "2026-05-04T10:00:00+08:00",
                    "delete_expires_at": "2026-05-07T10:00:00+08:00",
                }
            ],
            cloud_user={"workspace_id": 1, "role": "operator"},
            base_url="https://api.example.com",
        )

        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(config["tasks"], [])
        self.assertEqual(len(config["deleted_tasks"]), 1)
        tombstone = config["deleted_tasks"][0]
        self.assertEqual(tombstone["cloud_task_id"], 5)
        self.assertEqual(tombstone["deleted_at"], "2026-05-04T10:00:00+08:00")
        self.assertEqual(tombstone["expires_at"], "2026-05-07T10:00:00+08:00")
        self.assertEqual(tombstone["task"]["task_id"], "cloud_5")

    def test_deleted_cloud_task_without_local_copy_creates_restore_backup(self):
        config = {"tasks": []}

        summary = merge_cloud_tasks_into_config(
            config,
            [],
            deleted_cloud_tasks=[
                {
                    "id": 9,
                    "workspace_id": 1,
                    "task_key": "deleted-task",
                    "name": "已删任务",
                    "brand": "已删品牌",
                    "config_json": {"keywords": ["已删品牌"], "platforms": ["kimi"]},
                    "config_version": 3,
                    "enabled": True,
                    "access_level": "operate",
                    "deleted_at": "2026-05-04T10:00:00+08:00",
                    "delete_expires_at": "2026-05-07T10:00:00+08:00",
                }
            ],
            cloud_user={"workspace_id": 1, "role": "operator"},
            base_url="https://api.example.com",
        )

        self.assertEqual(summary["deleted_backups"], 1)
        self.assertEqual(config["tasks"], [])
        self.assertEqual(config["deleted_tasks"][0]["cloud_task_id"], 9)
        self.assertEqual(config["deleted_tasks"][0]["task"]["name"], "已删任务")

    def test_pull_refreshes_token_once_after_401(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save(
                {
                    "base_url": "https://api.example.com",
                    "access_token": "old-access",
                    "refresh_token": "refresh",
                    "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                }
            )
            client = FakeTaskClient(
                [
                    {
                        "id": 1,
                        "workspace_id": 1,
                        "task_key": "brand-demo",
                        "name": "测试品牌任务",
                        "brand": "测试品牌",
                        "config_json": {"keywords": ["测试品牌"], "platforms": ["kimi"]},
                        "config_version": 1,
                        "enabled": True,
                        "access_level": "operate",
                    }
                ],
                fail_once_401=True,
            )
            config = {"tasks": []}

            result = pull_cloud_tasks_into_config(config, client=client, session_store=store)

            self.assertTrue(result["ok"])
            self.assertEqual(client.refresh_calls, 1)
            self.assertEqual(client.list_tokens, ["old-access", "new-access"])
            self.assertEqual(store.load()["access_token"], "new-access")
            self.assertEqual(config["tasks"][0]["cloud_task_id"], 1)

    def test_pull_imports_cloud_run_records_for_trend_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_history_dir = history_module.HISTORY_DIR
            history_module.HISTORY_DIR = Path(tmpdir) / "logs" / "history"
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            try:
                store.save(
                    {
                        "base_url": "https://api.example.com",
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                    }
                )
                client = FakeTaskClient(
                    [
                        {
                            "id": 9,
                            "workspace_id": 1,
                            "task_key": "brand-demo",
                            "name": "趋势品牌",
                            "brand": "趋势品牌",
                            "config_json": {"keywords": ["趋势品牌"], "platforms": ["doubao"]},
                            "config_version": 1,
                            "enabled": True,
                            "access_level": "operate",
                        }
                    ],
                    run_records_by_task={
                        9: [
                            {
                                "id": 12,
                                "task_id": 9,
                                "platform": "doubao",
                                "keyword": "趋势品牌",
                                "brand": "趋势品牌",
                                "mode": "browser",
                                "result_json": {"rank": 1, "success": True, "highlight_count": 2},
                                "idempotency_key": "run:cloud-record-12",
                                "executed_at": "2026-05-03T08:00:00Z",
                            }
                        ]
                    },
                )
                config = {"tasks": []}

                result = pull_cloud_tasks_into_config(config, client=client, session_store=store)

                self.assertTrue(result["ok"])
                self.assertEqual(result["summary"]["run_records"]["imported"], 1)
                self.assertEqual(client.run_record_calls, [("access", 9, 6000, 0)])
                self.assertEqual(config["tasks"][0]["cloud_last_run_record_synced_id"], 12)
                records = history_module.get_records("趋势品牌", task_id="cloud_9")
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["id"], "cloud:run:cloud-record-12")
                self.assertEqual(records[0]["answer_text"], "")
                self.assertEqual(records[0]["rank"], 1)
                self.assertTrue(records[0]["success"])
            finally:
                history_module.HISTORY_DIR = original_history_dir

    def test_pull_refreshes_when_run_records_token_expires(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_history_dir = history_module.HISTORY_DIR
            history_module.HISTORY_DIR = Path(tmpdir) / "logs" / "history"
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            try:
                store.save(
                    {
                        "base_url": "https://api.example.com",
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                    }
                )
                client = FakeTaskClient(
                    [
                        {
                            "id": 9,
                            "workspace_id": 1,
                            "task_key": "brand-demo",
                            "name": "趋势品牌",
                            "brand": "趋势品牌",
                            "config_json": {"keywords": ["趋势品牌"], "platforms": ["doubao"]},
                            "config_version": 1,
                            "enabled": True,
                            "access_level": "operate",
                        }
                    ],
                    fail_run_records_once_401=True,
                    run_records_by_task={
                        9: [
                            {
                                "id": 14,
                                "task_id": 9,
                                "platform": "doubao",
                                "keyword": "趋势品牌",
                                "brand": "趋势品牌",
                                "mode": "browser",
                                "result_json": {"rank": 1, "success": True},
                                "idempotency_key": "run:cloud-record-14",
                                "executed_at": "2026-05-03T08:00:00Z",
                            }
                        ]
                    },
                )
                config = {"tasks": []}

                result = pull_cloud_tasks_into_config(config, client=client, session_store=store)

                self.assertTrue(result["ok"])
                self.assertEqual(client.refresh_calls, 1)
                self.assertEqual(
                    client.run_record_calls,
                    [("access", 9, 6000, 0), ("new-access", 9, 6000, 0)],
                )
                self.assertEqual(result["summary"]["run_records"]["imported"], 1)
                self.assertEqual(store.load()["access_token"], "new-access")
            finally:
                history_module.HISTORY_DIR = original_history_dir

    def test_pull_uses_run_record_cursor_after_first_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_history_dir = history_module.HISTORY_DIR
            history_module.HISTORY_DIR = Path(tmpdir) / "logs" / "history"
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            try:
                store.save(
                    {
                        "base_url": "https://api.example.com",
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                    }
                )
                cloud_task = {
                    "id": 9,
                    "workspace_id": 1,
                    "task_key": "brand-demo",
                    "name": "趋势品牌",
                    "brand": "趋势品牌",
                    "config_json": {"keywords": ["趋势品牌"], "platforms": ["doubao"]},
                    "config_version": 1,
                    "enabled": True,
                    "access_level": "operate",
                }
                client = FakeTaskClient(
                    [cloud_task],
                    run_records_by_task={
                        9: [
                            {
                                "id": 12,
                                "task_id": 9,
                                "platform": "doubao",
                                "keyword": "旧记录",
                                "brand": "趋势品牌",
                                "mode": "browser",
                                "result_json": {"rank": 1, "success": True},
                                "idempotency_key": "run:old",
                                "executed_at": "2026-05-02T08:00:00Z",
                            },
                            {
                                "id": 13,
                                "task_id": 9,
                                "platform": "doubao",
                                "keyword": "新记录",
                                "brand": "趋势品牌",
                                "mode": "browser",
                                "result_json": {"rank": 2, "success": True},
                                "idempotency_key": "run:new",
                                "executed_at": "2026-05-03T08:00:00Z",
                            },
                        ]
                    },
                )
                config = {
                    "tasks": [
                        {
                            "task_id": "cloud_9",
                            "cloud_task_id": 9,
                            "cloud_task_key": "brand-demo",
                            "name": "趋势品牌",
                            "brand": "趋势品牌",
                            "cloud_last_run_record_synced_id": 12,
                        }
                    ]
                }

                result = pull_cloud_tasks_into_config(config, client=client, session_store=store)

                self.assertTrue(result["ok"])
                self.assertEqual(client.run_record_calls, [("access", 9, 6000, 12)])
                self.assertEqual(result["summary"]["run_records"]["imported"], 1)
                self.assertEqual(config["tasks"][0]["cloud_last_run_record_synced_id"], 13)
                records = history_module.get_records("趋势品牌", task_id="cloud_9")
                self.assertEqual([record["keyword"] for record in records], ["新记录"])
            finally:
                history_module.HISTORY_DIR = original_history_dir

    def test_pull_imports_run_records_for_deleted_task_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_history_dir = history_module.HISTORY_DIR
            history_module.HISTORY_DIR = Path(tmpdir) / "logs" / "history"
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            try:
                store.save(
                    {
                        "base_url": "https://api.example.com",
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                    }
                )
                deleted_cloud_task = {
                    "id": 9,
                    "workspace_id": 1,
                    "task_key": "brand-demo",
                    "name": "趋势品牌",
                    "brand": "趋势品牌",
                    "config_json": {"keywords": ["趋势品牌"], "platforms": ["doubao"]},
                    "config_version": 2,
                    "enabled": True,
                    "access_level": "operate",
                    "deleted_at": "2026-05-04T10:00:00+08:00",
                    "delete_expires_at": "2026-05-07T10:00:00+08:00",
                }
                client = FakeTaskClient(
                    [],
                    deleted_tasks=[deleted_cloud_task],
                    run_records_by_task={
                        9: [
                            {
                                "id": 13,
                                "task_id": 9,
                                "platform": "doubao",
                                "keyword": "删除后补传",
                                "brand": "趋势品牌",
                                "mode": "browser",
                                "result_json": {"rank": 2, "success": True},
                                "idempotency_key": "run:deleted-new",
                                "executed_at": "2026-05-04T11:00:00Z",
                            }
                        ]
                    },
                )
                config = {
                    "tasks": [
                        {
                            "task_id": "cloud_9",
                            "cloud_task_id": 9,
                            "cloud_task_key": "brand-demo",
                            "name": "趋势品牌",
                            "brand": "趋势品牌",
                            "cloud_workspace_id": 1,
                            "cloud_base_url": "https://api.example.com",
                            "cloud_last_run_record_synced_id": 12,
                        }
                    ]
                }

                result = pull_cloud_tasks_into_config(config, client=client, session_store=store)

                self.assertTrue(result["ok"])
                self.assertEqual(result["summary"]["deleted"], 1)
                self.assertEqual(result["summary"]["run_records"]["imported"], 1)
                self.assertEqual(client.list_deleted_tokens, ["access"])
                self.assertEqual(client.run_record_calls, [("access", 9, 6000, 12)])
                self.assertEqual(config["tasks"], [])
                backup_task = config["deleted_tasks"][0]["task"]
                self.assertEqual(backup_task["cloud_last_run_record_synced_id"], 13)
                records = history_module.get_records("趋势品牌", task_id="cloud_9")
                self.assertEqual([record["keyword"] for record in records], ["删除后补传"])
            finally:
                history_module.HISTORY_DIR = original_history_dir


if __name__ == "__main__":
    unittest.main()
