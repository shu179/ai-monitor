from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.cloud_client import CloudClientError
from core.cloud_outbox import CloudOutbox
from core.cloud_run_sync import (
    article_to_cloud_events,
    enqueue_article_cloud_sync,
    enqueue_cloud_articles,
    enqueue_recent_cloud_run_records_from_history,
    enqueue_run_record_from_history,
    enqueue_task_day_status,
    flush_cloud_outbox,
    history_record_to_reference_events,
    history_record_to_run_event,
    history_record_to_run_events,
)
from core.cloud_session_store import CloudSessionStore
import core.history as history_module


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


class BackpressureClient:
    def post_events(self, access_token: str, events: list[dict]) -> dict:
        del access_token, events
        raise CloudClientError(
            "queue overloaded",
            status_code=429,
            retry_after_seconds=12,
            queue_depth_hint=23000,
            throttle_bucket="sync_metadata",
        )


class CloudRunSyncTests(unittest.TestCase):
    def test_article_to_cloud_events_uploads_metadata_and_task_links(self):
        article = {
            "id": "article-1",
            "url": "http://www.example.com/a?utm_source=test",
            "title": "即搜AI 入选武汉 GEO 优化公司",
            "platform": "微信公众号",
            "media_name": "武汉观察",
            "media_type": "selfmedia",
            "published_at": "2026-05-06",
            "excerpt": "摘要" * 300,
            "answer_text": "本地运行回答不应该进入文章云端事件",
            "matched_tasks": ["即搜AI"],
            "match_reasons": {"即搜AI": ["标题包含品牌名"]},
        }
        config = {"tasks": [{"name": "即搜AI", "brand": "即搜AI", "task_id": "cloud_42", "cloud_task_id": 42}]}

        events = article_to_cloud_events(article, config)

        self.assertEqual([event["event_type"] for event in events], ["article_upsert", "article_task_links"])
        upsert = events[0]["payload"]
        self.assertEqual(upsert["canonical_url"], "https://example.com/a")
        self.assertEqual(upsert["title"], "即搜AI 入选武汉 GEO 优化公司")
        self.assertEqual(upsert["payload"]["local_article_id"], "article-1")
        self.assertLessEqual(len(upsert["payload"]["excerpt"]), 500)
        self.assertNotIn("answer_text", upsert["payload"])

        links = events[1]["payload"]
        self.assertEqual(links["task_ids"], [42])
        self.assertFalse(links["partial"])
        self.assertTrue(links["replace"])
        self.assertEqual(links["reason_json"]["42"], ["标题包含品牌名"])

    def test_article_to_cloud_events_skips_empty_links_and_keeps_partial_links(self):
        config = {"tasks": [{"name": "即搜AI", "cloud_task_id": 42}]}

        clear_events = article_to_cloud_events(
            {"id": "article-2", "url": "https://example.com/clear", "matched_tasks": []},
            config,
        )
        self.assertEqual([event["event_type"] for event in clear_events], ["article_upsert"])

        partial_events = article_to_cloud_events(
            {"id": "article-3", "url": "https://example.com/partial", "matched_tasks": ["还没上云的品牌"]},
            config,
        )
        self.assertEqual(partial_events[1]["payload"]["task_ids"], [])
        self.assertTrue(partial_events[1]["payload"]["partial"])
        self.assertEqual(partial_events[1]["payload"]["unresolved_task_names"], ["还没上云的品牌"])

    def test_enqueue_article_cloud_sync_dedupes_by_payload_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            article = {
                "id": "article-1",
                "url": "https://example.com/a",
                "title": "即搜AI",
                "matched_tasks": ["即搜AI"],
            }
            config = {"tasks": [{"name": "即搜AI", "cloud_task_id": 42}]}

            enqueue_article_cloud_sync(article, config, outbox=outbox)
            enqueue_article_cloud_sync(article, config, outbox=outbox)

            self.assertEqual(outbox.stats()["pending"], 2)

    def test_enqueue_cloud_articles_batches_outbox_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            articles = [
                {
                    "id": f"article-{index}",
                    "url": f"https://example.com/{index}",
                    "title": "即搜AI",
                    "matched_tasks": ["即搜AI"],
                }
                for index in range(3)
            ]
            config = {"tasks": [{"name": "即搜AI", "cloud_task_id": 42}]}

            result = enqueue_cloud_articles(articles, config, outbox=outbox)
            duplicate_result = enqueue_cloud_articles(articles, config, outbox=outbox)

            self.assertEqual(result["articles"], 3)
            self.assertEqual(result["events"], 6)
            self.assertEqual(result["queued"], 6)
            self.assertEqual(duplicate_result["queued"], 0)
            self.assertEqual(outbox.stats()["pending"], 6)

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

    def test_history_record_to_run_event_infers_cloud_task_id_from_local_task_id(self):
        record = {
            "id": "history-cloud-local-id",
            "task_id": "cloud_42",
            "ts": "2026-05-03 12:50",
            "platform": "doubao",
            "keyword": "测试品牌",
            "brand": "测试品牌",
            "rank": 1,
            "success": True,
        }

        event = history_record_to_run_event(record)

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["payload"]["task_id"], 42)

    def test_recognition_history_record_expands_to_real_query_events(self):
        record = {
            "id": "recognition-history-1",
            "task_id": "cloud_42",
            "ts": "2026-05-05 18:30",
            "platform": "recognition",
            "keyword": "clipboard",
            "brand": "即搜AI",
            "rank": 1,
            "success": True,
            "mode": "recognition",
            "extra": {
                "detected_platforms": ["doubao"],
                "matched_pairs": [
                    {
                        "keyword": "武汉GEO优化公司",
                        "brand": "即搜AI",
                        "platforms": ["doubao"],
                    }
                ],
            },
        }

        events = history_record_to_run_events(record)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event_type"], "run_record")
        self.assertNotEqual(event["idempotency_key"], "run:recognition-history-1")
        self.assertEqual(event["payload"]["task_id"], 42)
        self.assertEqual(event["payload"]["keyword"], "武汉GEO优化公司")
        self.assertEqual(event["payload"]["brand"], "即搜AI")
        self.assertEqual(event["payload"]["platform"], "doubao")
        self.assertEqual(event["payload"]["mode"], "recognition")

    def test_recognition_clipboard_without_matched_pairs_is_not_uploaded(self):
        record = {
            "id": "recognition-history-without-pairs",
            "task_id": "cloud_42",
            "ts": "2026-05-05 18:30",
            "platform": "recognition",
            "keyword": "clipboard",
            "brand": "即搜AI",
            "rank": 1,
            "success": True,
            "mode": "recognition",
            "extra": {"detected_platforms": ["doubao"]},
        }

        self.assertEqual(history_record_to_run_events(record), [])

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

    def test_manual_test_success_is_not_uploaded_as_run_record(self):
        record = {
            "id": "manual-test-success",
            "task_id": "cloud_42",
            "ts": "2026-05-06 12:00",
            "platform": "doubao",
            "keyword": "测试品牌",
            "brand": "测试品牌",
            "rank": 1,
            "success": True,
            "execution_source": "manual_test",
            "extra": {"references": [{"url": "https://example.com/a"}]},
        }

        self.assertEqual(history_record_to_run_events(record), [])
        self.assertEqual(history_record_to_reference_events(record), [])

    def test_enqueue_task_day_status_sanitizes_forced_send_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")

            queued = enqueue_task_day_status(
                {
                    "task_id": 42,
                    "task_day": "2026-05-06",
                    "status": "success",
                    "source": "dashboard_force_send",
                    "message": "已发送 1 张成功截图",
                    "brands": ["即搜AI", "即搜AI"],
                    "completed_keywords": ["武汉GEO优化公司"],
                    "detected_platforms": ["doubao"],
                    "image_count": 1,
                    "notification_success": True,
                    "forced_ignore_failure": True,
                    "screenshot_paths": ["/tmp/local-only.png"],
                },
                outbox=outbox,
            )

            self.assertIsNotNone(queued)
            pending = outbox.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["event_type"], "task_day_status")
            self.assertEqual(pending[0]["payload"]["task_id"], 42)
            self.assertEqual(pending[0]["payload"]["brands"], ["即搜AI"])
            self.assertTrue(pending[0]["payload"]["forced_ignore_failure"])
            self.assertNotIn("screenshot_paths", pending[0]["payload"])

    def test_flush_cloud_outbox_only_sends_latest_profile_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save({
                "base_url": "https://api.example.com",
                "access_token": "access",
                "refresh_token": "refresh",
                "user": {"id": 1, "workspace_id": 1, "role": "admin"},
            })
            outbox.enqueue(
                event_type="profile_update",
                idempotency_key="profile:old",
                payload={"display_name": "shuao"},
            )
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:1",
                payload={"task_id": 1, "platform": "doubao"},
            )
            outbox.enqueue(
                event_type="profile_update",
                idempotency_key="profile:new",
                payload={"display_name": "管理员"},
            )
            client = FakeCloudClient()

            result = flush_cloud_outbox(client=client, session_store=store, outbox=outbox)

            self.assertTrue(result["ok"])
            self.assertEqual([event["idempotency_key"] for event in client.events], ["run:1", "profile:new"])
            self.assertEqual(client.events[-1]["payload"]["display_name"], "管理员")
            self.assertEqual(outbox.stats()["pending"], 0)

    def test_enqueue_recent_cloud_run_records_from_history_recovers_local_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_dir = Path(tmpdir) / "history"
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            with patch.object(history_module, "HISTORY_DIR", history_dir):
                written = history_module.record(
                    "即搜AI",
                    "recognition",
                    "clipboard",
                    "即搜AI",
                    1,
                    True,
                    {
                        "mode": "recognition",
                        "extra": {
                            "detected_platforms": ["doubao"],
                            "matched_pairs": [
                                {
                                    "keyword": "武汉GEO优化公司",
                                    "brand": "即搜AI",
                                    "platforms": ["doubao"],
                                }
                            ],
                        },
                    },
                    task_id="cloud_42",
                )
                config = {
                    "tasks": [
                        {
                            "task_id": "cloud_42",
                            "cloud_task_id": 42,
                            "name": "即搜AI",
                            "brand": "即搜AI",
                        }
                    ]
                }

                result = enqueue_recent_cloud_run_records_from_history(config, outbox=outbox, days=7)

            self.assertTrue(written["id"])
            self.assertEqual(result["tasks"], 1)
            self.assertEqual(result["records"], 1)
            self.assertEqual(result["candidates"], 1)
            self.assertEqual(result["queued"], 1)
            pending = outbox.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["payload"]["task_id"], 42)
            self.assertEqual(pending[0]["payload"]["keyword"], "武汉GEO优化公司")
            self.assertEqual(pending[0]["payload"]["platform"], "doubao")

    def test_enqueue_recent_cloud_run_records_skips_cloud_imported_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_dir = Path(tmpdir) / "history"
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            with patch.object(history_module, "HISTORY_DIR", history_dir):
                history_module.import_records(
                    "即搜AI",
                    [
                        {
                            "id": "cloud:run:already-uploaded",
                            "ts": "2026-05-05 18:30",
                            "task_id": "cloud_42",
                            "task_name": "即搜AI",
                            "platform": "doubao",
                            "keyword": "武汉GEO优化公司",
                            "brand": "即搜AI",
                            "rank": 1,
                            "success": True,
                            "mode": "browser",
                            "execution_source": "cloud",
                            "extra": {
                                "cloud_task_id": 42,
                                "cloud_run_record_id": "88",
                                "cloud_idempotency_key": "run:operator-local",
                            },
                        }
                    ],
                    task_id="cloud_42",
                )
                config = {
                    "tasks": [
                        {
                            "task_id": "cloud_42",
                            "cloud_task_id": 42,
                            "name": "即搜AI",
                            "brand": "即搜AI",
                        }
                    ]
                }

                result = enqueue_recent_cloud_run_records_from_history(config, outbox=outbox, days=7)

            self.assertEqual(result["records"], 0)
            self.assertEqual(result["candidates"], 0)
            self.assertEqual(result["queued"], 0)
            self.assertEqual(outbox.stats()["pending"], 0)

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

    def test_flush_cloud_outbox_returns_backpressure_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save({
                "base_url": "https://api.example.com",
                "access_token": "access",
                "refresh_token": "refresh",
                "user": {"id": 2, "workspace_id": 1, "role": "operator"},
            })
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:backpressure",
                payload={"task_id": 1, "platform": "kimi"},
            )

            with patch("core.cloud_outbox.time.time", return_value=1000.0):
                result = flush_cloud_outbox(client=BackpressureClient(), session_store=store, outbox=outbox)
                pending = outbox.pending(limit=10)

            self.assertFalse(result["ok"])
            self.assertEqual(result["retry_after_seconds"], 12.0)
            self.assertEqual(result["queue_depth_hint"], 23000)
            self.assertEqual(result["throttle_bucket"], "sync_metadata")
            self.assertEqual(result["metrics"]["retry_after_seconds"], 12.0)
            self.assertEqual(result["metrics"]["queue_depth_hint"], 23000)
            self.assertEqual(result["metrics"]["throttle_bucket"], "sync_metadata")
            self.assertEqual(outbox.stats()["failed"], 1)
            self.assertEqual(pending, [])

            with patch("core.cloud_outbox.time.time", return_value=1013.0):
                retry_ready = outbox.pending(limit=10)
            self.assertEqual([item["idempotency_key"] for item in retry_ready], ["run:backpressure"])

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
