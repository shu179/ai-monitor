from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.cloud_state_delta import CloudStateDeltaStore, pull_cloud_state_delta
from core.cloud_client import CloudClientError
from core.cloud_state_delta_inbox import CloudStateDeltaInbox


def _session(access_token: str = "access-token") -> dict:
    return {
        "base_url": "https://api.example.com",
        "access_token": access_token,
        "refresh_token": "refresh-token",
        "user": {"id": 2, "workspace_id": 3},
    }


class FakeSessionStore:
    def __init__(self, session: dict | None = None) -> None:
        self._session = dict(session or {})
        self.refreshed = False

    def load(self) -> dict:
        return dict(self._session)

    def refresh_login_if_current(self, **kwargs) -> dict:
        self.refreshed = True
        self._session = _session("new-access-token")
        return dict(self._session)

    def clear_if_current(self, **kwargs) -> bool:
        self._session = {}
        return True


class FakeStateDeltaClient:
    def __init__(self, responses: list[dict] | None = None, fail_once: bool = False) -> None:
        self.responses = list(responses or [])
        self.fail_once = fail_once
        self.calls: list[dict] = []

    def state_delta(self, access_token: str, **kwargs) -> dict:
        self.calls.append({"access_token": access_token, **kwargs})
        if self.fail_once:
            self.fail_once = False
            raise CloudClientError("expired", status_code=401)
        if self.responses:
            return self.responses.pop(0)
        return {
            "changes": [],
            "next_cursors": {},
            "has_more": False,
            "object_refs": [],
            "reset_required": False,
            "retry_after_seconds": 0,
        }

    def refresh(self, _refresh_token: str) -> dict:
        return _session("new-access-token")


class CloudStateDeltaTests(unittest.TestCase):
    def test_store_resets_when_identity_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateDeltaStore(Path(tmp) / "state.json")
            store.save({"identity_key": "old", "cursors": {"tasks": 3}})

            state = store.load(_session())

        self.assertEqual(state["identity_key"], "https://api.example.com|3|2")
        self.assertEqual(state["cursors"], {})

    def test_pull_updates_cursors_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateDeltaStore(Path(tmp) / "state.json")
            inbox_path = Path(tmp) / "inbox.sqlite3"
            client = FakeStateDeltaClient(
                [
                    {
                        "changes": [
                            {"stream": "tasks", "seq": 1},
                            {"stream": "articles", "seq": 7},
                        ],
                        "next_cursors": {"tasks": 1, "articles": 7},
                        "has_more": False,
                        "object_refs": [{"object_id": "obj-1"}],
                        "reset_required": False,
                        "retry_after_seconds": 0,
                    }
                ]
            )

            result = pull_cloud_state_delta(
                client=client,
                session_store=FakeSessionStore(_session()),
                state_store=store,
                inbox=CloudStateDeltaInbox(inbox_path),
                limit=500,
                max_pages=2,
            )
            state = store.load(_session())

        self.assertTrue(result["ok"])
        self.assertEqual(result["changes"], 2)
        self.assertEqual(result["streams"], {"tasks": 1, "articles": 1})
        self.assertEqual(result["object_refs"], 1)
        self.assertEqual(result["inbox_created"], 2)
        self.assertEqual(result["inbox_duplicates"], 0)
        self.assertEqual(state["cursors"], {"tasks": 1, "articles": 7})
        self.assertEqual(state["reset_token"], "")
        self.assertEqual(state["last_summary"]["changes"], 2)
        self.assertEqual(state["last_summary"]["inbox_created"], 2)
        self.assertEqual(client.calls[0]["cursors"], {})

    def test_pull_saves_reset_required_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateDeltaStore(Path(tmp) / "state.json")
            client = FakeStateDeltaClient(
                [
                    {
                        "changes": [],
                        "next_cursors": {"tasks": 1},
                        "has_more": True,
                        "object_refs": [],
                        "reset_required": True,
                        "reset_token": "reset-token",
                        "bootstrap_cursor": "boot-cursor",
                        "retry_after_seconds": 5,
                    }
                ]
            )

            result = pull_cloud_state_delta(
                client=client,
                session_store=FakeSessionStore(_session()),
                state_store=store,
                max_pages=1,
            )
            state = store.load(_session())

        self.assertTrue(result["ok"])
        self.assertTrue(result["reset_required"])
        self.assertEqual(state["reset_token"], "reset-token")
        self.assertEqual(state["bootstrap_cursor"], "boot-cursor")

    def test_pull_continues_reset_bootstrap_with_remaining_page_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateDeltaStore(Path(tmp) / "state.json")
            client = FakeStateDeltaClient(
                [
                    {
                        "changes": [],
                        "next_cursors": {"tasks": 1},
                        "has_more": True,
                        "object_refs": [],
                        "reset_required": True,
                        "reset_token": "reset-token",
                        "bootstrap_cursor": "boot-cursor",
                        "retry_after_seconds": 5,
                    },
                    {
                        "changes": [{"stream": "tasks", "seq": 9}],
                        "next_cursors": {"tasks": 9},
                        "has_more": False,
                        "object_refs": [],
                        "reset_required": False,
                        "reset_token": None,
                        "bootstrap_cursor": None,
                        "retry_after_seconds": 0,
                    },
                ]
            )
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")

            result = pull_cloud_state_delta(
                client=client,
                session_store=FakeSessionStore(_session()),
                state_store=store,
                inbox=inbox,
                max_pages=2,
            )
            state = store.load(_session())

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "reset")
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["changes"], 1)
        self.assertEqual(result["inbox_created"], 1)
        self.assertTrue(result["reset_required"])
        self.assertFalse(result["has_more"])
        self.assertEqual(state["cursors"], {"tasks": 9})
        self.assertEqual(state["reset_token"], "")
        self.assertEqual(state["bootstrap_cursor"], "")
        self.assertIsNone(client.calls[0]["reset_token"])
        self.assertEqual(client.calls[1]["reset_token"], "reset-token")
        self.assertEqual(client.calls[1]["bootstrap_cursor"], "boot-cursor")

    def test_pull_saves_reset_token_when_page_budget_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateDeltaStore(Path(tmp) / "state.json")
            client = FakeStateDeltaClient(
                [
                    {
                        "changes": [],
                        "next_cursors": {"tasks": 1},
                        "has_more": True,
                        "object_refs": [],
                        "reset_required": True,
                        "reset_token": "reset-token",
                        "bootstrap_cursor": "boot-cursor",
                    },
                ]
            )

            result = pull_cloud_state_delta(
                client=client,
                session_store=FakeSessionStore(_session()),
                state_store=store,
                max_pages=1,
            )
            state = store.load(_session())

        self.assertTrue(result["ok"])
        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["reset_required"])
        self.assertTrue(result["has_more"])
        self.assertEqual(state["reset_token"], "reset-token")
        self.assertEqual(state["bootstrap_cursor"], "boot-cursor")

    def test_reset_page_clears_reset_state_when_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateDeltaStore(Path(tmp) / "state.json")
            store.save(
                {
                    "identity_key": "https://api.example.com|3|2",
                    "cursors": {"tasks": 1},
                    "reset_token": "reset-token",
                    "bootstrap_cursor": "boot-cursor",
                }
            )
            client = FakeStateDeltaClient(
                [
                    {
                        "changes": [{"stream": "tasks", "seq": 9}],
                        "next_cursors": {"tasks": 9},
                        "has_more": False,
                        "object_refs": [],
                        "reset_required": False,
                        "reset_token": None,
                        "bootstrap_cursor": None,
                        "retry_after_seconds": 0,
                    }
                ]
            )

            result = pull_cloud_state_delta(
                client=client,
                session_store=FakeSessionStore(_session()),
                state_store=store,
            )
            state = store.load(_session())

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "reset")
        self.assertEqual(state["cursors"], {"tasks": 9})
        self.assertEqual(state["reset_token"], "")
        self.assertEqual(state["bootstrap_cursor"], "")
        self.assertEqual(client.calls[0]["reset_token"], "reset-token")
        self.assertEqual(client.calls[0]["bootstrap_cursor"], "boot-cursor")

    def test_pull_refreshes_on_401(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateDeltaStore(Path(tmp) / "state.json")
            session_store = FakeSessionStore(_session())
            client = FakeStateDeltaClient(
                [
                    {
                        "changes": [],
                        "next_cursors": {"tasks": 4},
                        "has_more": False,
                        "object_refs": [],
                        "reset_required": False,
                    }
                ],
                fail_once=True,
            )

            result = pull_cloud_state_delta(
                client=client,
                session_store=session_store,
                state_store=store,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(session_store.refreshed)
        self.assertEqual(client.calls[0]["access_token"], "access-token")
        self.assertEqual(client.calls[1]["access_token"], "new-access-token")

    def test_no_login_returns_error_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = pull_cloud_state_delta(
                client=FakeStateDeltaClient(),
                session_store=FakeSessionStore({}),
                state_store=CloudStateDeltaStore(Path(tmp) / "state.json"),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "未登录云端")

    def test_pull_records_backpressure_from_cloud_error(self) -> None:
        class BackpressureClient(FakeStateDeltaClient):
            def state_delta(self, access_token: str, **kwargs) -> dict:
                self.calls.append({"access_token": access_token, **kwargs})
                raise CloudClientError(
                    "queue overloaded",
                    status_code=429,
                    retry_after_seconds=12,
                    queue_depth_hint=23000,
                    throttle_bucket="state_delta",
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateDeltaStore(Path(tmp) / "state.json")
            result = pull_cloud_state_delta(
                client=BackpressureClient(),
                session_store=FakeSessionStore(_session()),
                state_store=store,
            )
            state = store.load(_session())

        self.assertFalse(result["ok"])
        self.assertEqual(result["retry_after_seconds"], 12.0)
        self.assertEqual(result["next_retry_after_seconds"], 12.0)
        self.assertEqual(result["queue_depth_hint"], 23000)
        self.assertEqual(result["throttle_bucket"], "state_delta")
        self.assertEqual(state["last_summary"]["queue_depth_hint"], 23000)


if __name__ == "__main__":
    unittest.main()
