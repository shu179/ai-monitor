from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.cloud_client import CloudClientError
from core.cloud_outbox import CloudOutbox
from core.cloud_run_sync import (
    enqueue_run_record_from_history,
    flush_cloud_outbox,
    history_record_to_reference_events,
    history_record_to_run_event,
)
from core.cloud_session_store import CloudSessionStore


class FakeCloudClient:
    def __init__(self, *, fail_once_401: bool = False) -> None:
        self.fail_once_401 = fail_once_401
        self.events: list[dict] = []
        self.refresh_calls = 0

    def post_events(self, access_token: str, events: list[dict]) -> dict:
        if self.fail_once_401:
            self.fail_once_401 = False
            raise CloudClientError("expired", status_code=401)
        self.events.extend(events)
        return {"accepted": len(events), "duplicates": 0, "token": access_token}

    def refresh(self, refresh_token: str) -> dict:
        self.refresh_calls += 1
        return {
            "access_token": "new-access",
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {"id": 2, "role": "operator"},
        }


class ConcurrentRefreshClient:
    def __init__(self, store: CloudSessionStore) -> None:
        self.store = store
        self.events: list[dict] = []
        self.refresh_calls = 0
        self.post_tokens: list[str] = []

    def post_events(self, access_token: str, events: list[dict]) -> dict:
        self.post_tokens.append(access_token)
        if access_token == "old-access":
            self.store.save({
                "base_url": "https://api.example.com",
                "access_token": "concurrent-access",
                "refresh_token": "concurrent-refresh",
                "user": {"id": 2, "role": "operator"},
            })
            raise CloudClientError("expired", status_code=401)
        self.events.extend(events)
        return {"accepted": len(events), "duplicates": 0, "token": access_token}

    def refresh(self, refresh_token: str) -> dict:
        self.refresh_calls += 1
        raise CloudClientError("refresh token revoked", status_code=401)


class AccountSwitchDuringUploadClient:
    def __init__(self, store: CloudSessionStore) -> None:
        self.store = store
        self.refresh_calls = 0

    def post_events(self, access_token: str, events: list[dict]) -> dict:
        del events
        if access_token == "old-access":
            self.store.save({
                "base_url": "https://api.example.com",
                "access_token": "other-access",
                "refresh_token": "other-refresh",
                "user": {"id": 3, "workspace_id": 1, "role": "operator"},
            })
            raise CloudClientError("expired", status_code=401)
        raise AssertionError("old upload must not retry with another account token")

    def refresh(self, refresh_token: str) -> dict:
        del refresh_token
        self.refresh_calls += 1
        raise AssertionError("account switches should not refresh the old token")


class CloudRunSyncTests(unittest.TestCase):
    def test_history_record_to_run_event_requires_cloud_task_id_and_omits_screenshot(self):
        record = {
            "id": "history-1",
            "ts": "2026-05-03 12:50",
            "platform": "Kimi",
            "keyword": "测试品牌",
            "brand": "测试品牌",
            "rank": 1,
            "success": True,
            "screenshot": "/tmp/secret.png",
            "answer_text": "这是一段不应该进入云端运行记录的完整回答正文",
            "evidence": "命中答案片段",
            "mode": "browser",
            "extra": {
                "reference_count": 1,
                "body_reference_count": 1,
                "total_reference_count": 2,
                "references": [{"url": "https://example.com/a"}],
                "body_references": [{"url": "https://example.com/b"}],
            },
        }

        self.assertIsNone(history_record_to_run_event(record))
        event = history_record_to_run_event(record, cloud_task_id=1)

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["event_type"], "run_record")
        self.assertEqual(event["idempotency_key"], "run:history-1")
        self.assertEqual(event["payload"]["task_id"], 1)
        self.assertEqual(event["payload"]["platform"], "kimi")
        self.assertNotIn("screenshot", event["payload"])
        self.assertNotIn("screenshot", event["payload"]["result"])
        self.assertNotIn("answer_text", event["payload"]["result"])
        self.assertNotIn("evidence", event["payload"]["result"])
        self.assertNotIn("references", event["payload"]["result"])
        self.assertNotIn("body_references", event["payload"]["result"])
        self.assertTrue(event["payload"]["run_started_at"].startswith("2026-05-03T12:50:00"))
        self.assertEqual(event["payload"]["result"]["total_reference_count"], 2)

        reference_events = history_record_to_reference_events(record, cloud_task_id=1)
        self.assertEqual(len(reference_events), 2)
        self.assertEqual(reference_events[0]["event_type"], "article_reference_event")
        self.assertTrue(reference_events[0]["payload"]["run_started_at"].startswith("2026-05-03T12:50:00"))
        self.assertEqual(reference_events[0]["payload"]["normalized_url"], "https://example.com/a")

    def test_history_record_to_run_event_preserves_explicit_run_started_at(self):
        record = {
            "id": "history-explicit-start",
            "ts": "2026-05-03 12:50",
            "platform": "doubao",
            "keyword": "测试品牌",
            "brand": "测试品牌",
            "rank": 1,
            "success": True,
            "extra": {"run_started_at": "2026-05-03T08:00:00+08:00"},
        }

        event = history_record_to_run_event(record, cloud_task_id=1)

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["payload"]["run_started_at"], "2026-05-03T08:00:00+08:00")

    def test_cloud_outbox_dedupes_run_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            record = {
                "id": "history-1",
                "ts": "2026-05-03 12:50",
                "platform": "kimi",
                "keyword": "测试品牌",
                "brand": "测试品牌",
                "rank": 1,
                "success": True,
            }

            first = enqueue_run_record_from_history(record, cloud_task_id=1, outbox=outbox)
            second = enqueue_run_record_from_history(record, cloud_task_id=1, outbox=outbox)

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(outbox.stats()["pending"], 1)

    def test_flush_cloud_outbox_marks_events_sent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save({
                "base_url": "https://api.example.com",
                "access_token": "access",
                "refresh_token": "refresh",
                "user": {"id": 2, "role": "operator"},
            })
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:1",
                payload={"task_id": 1, "platform": "kimi"},
            )
            client = FakeCloudClient()

            result = flush_cloud_outbox(client=client, session_store=store, outbox=outbox)

            self.assertTrue(result["ok"])
            self.assertEqual(len(client.events), 1)
            self.assertEqual(outbox.stats()["sent"], 1)

    def test_flush_cloud_outbox_sanitizes_legacy_run_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save({
                "base_url": "https://api.example.com",
                "access_token": "access",
                "refresh_token": "refresh",
                "user": {"id": 2, "role": "operator"},
            })
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:legacy",
                payload={
                    "task_id": 1,
                    "platform": "kimi",
                    "result": {
                        "rank": 1,
                        "success": True,
                        "evidence": "命中答案片段",
                        "answer_text": "完整回答正文",
                        "references": [{"url": "https://example.com/a"}],
                    },
                },
            )
            client = FakeCloudClient()

            result = flush_cloud_outbox(client=client, session_store=store, outbox=outbox)

            self.assertTrue(result["ok"])
            uploaded_result = client.events[0]["payload"]["result"]
            self.assertNotIn("answer_text", uploaded_result)
            self.assertNotIn("evidence", uploaded_result)
            self.assertNotIn("references", uploaded_result)
            self.assertEqual(uploaded_result["rank"], 1)

    def test_flush_cloud_outbox_refreshes_once_after_401(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save({
                "base_url": "https://api.example.com",
                "access_token": "old-access",
                "refresh_token": "refresh",
                "user": {"id": 2, "role": "operator"},
            })
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:1",
                payload={"task_id": 1, "platform": "kimi"},
            )
            client = FakeCloudClient(fail_once_401=True)

            result = flush_cloud_outbox(client=client, session_store=store, outbox=outbox)

            self.assertTrue(result["ok"])
            self.assertEqual(client.refresh_calls, 1)
            self.assertEqual(store.load()["access_token"], "new-access")
            self.assertEqual(outbox.stats()["sent"], 1)

    def test_flush_cloud_outbox_reuses_concurrently_refreshed_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save({
                "base_url": "https://api.example.com",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "user": {"id": 2, "role": "operator"},
            })
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:1",
                payload={"task_id": 1, "platform": "kimi"},
            )
            client = ConcurrentRefreshClient(store)

            result = flush_cloud_outbox(client=client, session_store=store, outbox=outbox)

            self.assertTrue(result["ok"])
            self.assertEqual(client.refresh_calls, 0)
            self.assertEqual(client.post_tokens, ["old-access", "concurrent-access"])
            self.assertEqual(store.load()["access_token"], "concurrent-access")
            self.assertEqual(outbox.stats()["sent"], 1)

    def test_flush_cloud_outbox_stays_bound_when_account_switches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = CloudSessionStore(root / "session.json")
            store.save({
                "base_url": "https://api.example.com",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "user": {"id": 2, "workspace_id": 1, "role": "operator"},
            })

            def fake_profile_dir(session: dict | None) -> Path | None:
                user = session.get("user") if isinstance(session, dict) and isinstance(session.get("user"), dict) else {}
                user_id = str(user.get("id") or "").strip()
                return root / f"profile-{user_id}" if user_id else None

            old_outbox_path = root / "profile-2" / "user_data" / "cloud_outbox.json"
            CloudOutbox(old_outbox_path).enqueue(
                event_type="run_record",
                idempotency_key="run:old-account",
                payload={"task_id": 1, "platform": "kimi"},
            )
            client = AccountSwitchDuringUploadClient(store)

            with patch("core.cloud_outbox.account_profile_dir_from_session", side_effect=fake_profile_dir):
                result = flush_cloud_outbox(client=client, session_store=store, outbox=CloudOutbox())

            self.assertFalse(result["ok"])
            self.assertIn("云端账号已切换", result["message"])
            self.assertEqual(client.refresh_calls, 0)
            self.assertEqual(CloudOutbox(old_outbox_path).stats()["pending"], 1)
            self.assertEqual(store.load()["user"]["id"], 3)


if __name__ == "__main__":
    unittest.main()
