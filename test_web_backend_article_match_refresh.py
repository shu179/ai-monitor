from __future__ import annotations

import copy
import threading
import unittest
from unittest.mock import patch

from web_backend import AppRuntime


class _FakeCloudSessionStore:
    def __init__(self, session: dict | None = None) -> None:
        self._session = copy.deepcopy(session or {})

    def load(self) -> dict:
        return copy.deepcopy(self._session)


def _runtime() -> AppRuntime:
    runtime = AppRuntime.__new__(AppRuntime)
    runtime._article_cache_lock = threading.RLock()
    runtime._synced_articles_cache = None
    runtime._article_cloud_enqueue_lock = threading.RLock()
    runtime._last_article_cloud_enqueue_key = None
    runtime._article_cloud_enqueue_requested = False
    runtime._article_cloud_enqueue_thread = None
    runtime._article_cloud_enqueue_retry_thread = None
    return runtime


def _config() -> dict:
    return {
        "tasks": [
            {
                "name": "BrandA",
                "brand": "BrandA",
                "cloud_task_id": 42,
                "cloud_access_level": "operate",
            }
        ]
    }


def _article(title: str, matched_tasks: list[str] | None = None) -> dict:
    return {
        "title": title,
        "matched_tasks": list(matched_tasks or []),
        "match_reasons": {},
    }


def _cloud_session() -> dict:
    return {
        "access_token": "token",
        "user": {"id": 2, "workspace_id": 1, "role": "admin"},
    }


class _FakeCloudOutbox:
    def __init__(self) -> None:
        self.bound_session: dict | None = None

    def bind_to_session(self, session: dict) -> "_FakeCloudOutbox":
        self.bound_session = copy.deepcopy(session)
        return self


class WebBackendArticleMatchRefreshTests(unittest.TestCase):
    def test_get_synced_articles_does_not_cache_deferred_snapshot(self) -> None:
        runtime = _runtime()
        stale_articles = [_article("stale match result")]
        fresh_articles = [_article("fresh match result", ["BrandA"])]

        with (
            patch("web_backend.CloudSessionStore", return_value=_FakeCloudSessionStore()),
            patch(
                "web_backend.schedule_article_match_refresh",
                side_effect=[
                    {"scheduled": True, "reason": "cloud_sync_read"},
                    {"scheduled": False, "reason": "nothing_to_refresh"},
                ],
            ) as schedule_refresh,
            patch("web_backend.get_articles", return_value=copy.deepcopy(stale_articles)) as get_articles_mock,
            patch("web_backend.refresh_article_matches", return_value=copy.deepcopy(fresh_articles)) as refresh_mock,
        ):
            first = runtime._get_synced_articles(_config())
            self.assertEqual([item["title"] for item in first], ["stale match result"])
            self.assertIsNone(runtime._synced_articles_cache)

            second = runtime._get_synced_articles(_config())

        self.assertEqual([item["title"] for item in second], ["fresh match result"])
        self.assertEqual(schedule_refresh.call_count, 2)
        get_articles_mock.assert_called_once()
        refresh_mock.assert_called_once()
        self.assertIsNotNone(runtime._synced_articles_cache)

    def test_get_synced_articles_already_running_uses_snapshot_without_sync_refresh(self) -> None:
        runtime = _runtime()
        snapshot_articles = [_article("running snapshot")]

        with (
            patch("web_backend.CloudSessionStore", return_value=_FakeCloudSessionStore()),
            patch(
                "web_backend.schedule_article_match_refresh",
                return_value={"scheduled": False, "reason": "already_running"},
            ),
            patch("web_backend.get_articles", return_value=copy.deepcopy(snapshot_articles)) as get_articles_mock,
            patch(
                "web_backend.refresh_article_matches",
                side_effect=AssertionError("already_running must not sync refresh"),
            ) as refresh_mock,
        ):
            result = runtime._get_synced_articles(_config())

        self.assertEqual([item["title"] for item in result], ["running snapshot"])
        get_articles_mock.assert_called_once()
        refresh_mock.assert_not_called()
        self.assertIsNone(runtime._synced_articles_cache)

    def test_cloud_upload_snapshot_already_running_uses_snapshot_without_sync_refresh(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        snapshot_articles = [_article("cloud snapshot")]

        with (
            patch(
                "web_backend.schedule_article_match_refresh",
                return_value={"scheduled": False, "reason": "already_running"},
            ),
            patch("web_backend.get_articles", return_value=copy.deepcopy(snapshot_articles)) as get_articles_mock,
            patch(
                "web_backend.refresh_article_matches",
                side_effect=AssertionError("already_running must not sync refresh"),
            ) as refresh_mock,
        ):
            result = runtime._get_cloud_article_upload_snapshot(
                _config(),
                session={"user": {"role": "admin"}},
            )

        self.assertIsInstance(result, list)
        self.assertEqual([item["title"] for item in result], ["cloud snapshot"])
        get_articles_mock.assert_called_once()
        refresh_mock.assert_not_called()

    def test_enqueue_cloud_articles_snapshot_skips_deferred_snapshot_and_schedules_retry(self) -> None:
        runtime = _runtime()
        stale_articles = [_article("stale cloud snapshot")]

        with (
            patch("web_backend.CloudSessionStore", return_value=_FakeCloudSessionStore(_cloud_session())),
            patch(
                "web_backend.schedule_article_match_refresh",
                return_value={"scheduled": True, "reason": "cloud_upload_snapshot"},
            ),
            patch("web_backend.get_articles", return_value=copy.deepcopy(stale_articles)) as get_articles_mock,
            patch(
                "web_backend.refresh_article_matches",
                side_effect=AssertionError("deferred refresh must not sync refresh"),
            ) as refresh_mock,
            patch("web_backend.enqueue_cloud_articles") as enqueue_mock,
            patch.object(runtime, "_schedule_cloud_articles_snapshot_retry") as retry_mock,
        ):
            runtime._enqueue_cloud_articles_snapshot(_config())

        get_articles_mock.assert_called_once()
        refresh_mock.assert_not_called()
        enqueue_mock.assert_not_called()
        retry_mock.assert_called_once()
        self.assertIsNone(runtime._last_article_cloud_enqueue_key)

    def test_deferred_retry_thread_guard_reuses_existing_timer(self) -> None:
        runtime = _runtime()
        created_threads: list[object] = []

        class FakeAliveThread:
            def __init__(self, *args, **kwargs) -> None:
                self.args = args
                self.kwargs = kwargs
                self.started = False
                created_threads.append(self)

            def start(self) -> None:
                self.started = True

            def is_alive(self) -> bool:
                return self.started

        with patch("web_backend.threading.Thread", side_effect=lambda *args, **kwargs: FakeAliveThread(*args, **kwargs)):
            runtime._schedule_cloud_articles_snapshot_retry(delay_seconds=0)
            runtime._schedule_cloud_articles_snapshot_retry(delay_seconds=0)

        self.assertEqual(len(created_threads), 1)
        self.assertEqual(created_threads[0].kwargs.get("name"), "cloud-article-snapshot-retry")
        self.assertTrue(created_threads[0].kwargs.get("daemon"))

    def test_enqueue_cloud_articles_snapshot_uploads_fresh_after_deferred_retry(self) -> None:
        runtime = _runtime()
        stale_articles = [_article("stale cloud snapshot")]
        fresh_articles = [_article("fresh cloud snapshot", ["BrandA"])]

        with (
            patch("web_backend.CloudSessionStore", return_value=_FakeCloudSessionStore(_cloud_session())),
            patch.object(runtime, "_article_store_version_key", return_value=("articles.json", 1, 100)),
            patch(
                "web_backend.schedule_article_match_refresh",
                side_effect=[
                    {"scheduled": True, "reason": "cloud_upload_snapshot"},
                    {"scheduled": False, "reason": "nothing_to_refresh"},
                ],
            ),
            patch("web_backend.get_articles", return_value=copy.deepcopy(stale_articles)),
            patch("web_backend.refresh_article_matches", return_value=copy.deepcopy(fresh_articles)) as refresh_mock,
            patch("web_backend.enqueue_cloud_articles", return_value={"articles": 1, "queued": 1}) as enqueue_mock,
            patch.object(runtime, "_schedule_cloud_articles_snapshot_retry") as retry_mock,
        ):
            runtime._enqueue_cloud_articles_snapshot(_config())
            self.assertIsNone(runtime._last_article_cloud_enqueue_key)
            enqueue_mock.assert_not_called()
            retry_mock.assert_called_once()

            runtime._enqueue_cloud_articles_snapshot(_config())

        refresh_mock.assert_called_once()
        enqueue_mock.assert_called_once()
        uploaded_articles = enqueue_mock.call_args.args[0]
        self.assertEqual([item["title"] for item in uploaded_articles], ["fresh cloud snapshot"])
        self.assertIsNotNone(runtime._last_article_cloud_enqueue_key)

    def test_recover_cloud_run_history_uploads_skips_deferred_article_snapshot(self) -> None:
        runtime = _runtime()
        runtime._lock = threading.RLock()
        runtime.load_config = lambda: copy.deepcopy(_config())
        fake_outbox = _FakeCloudOutbox()
        stale_articles = [_article("stale recovery snapshot")]

        with (
            patch("web_backend.CloudSessionStore", return_value=_FakeCloudSessionStore(_cloud_session())),
            patch("web_backend.CloudOutbox", return_value=fake_outbox),
            patch(
                "web_backend.enqueue_recent_cloud_run_records_from_history",
                return_value={"run_queued": 2},
            ) as run_enqueue_mock,
            patch(
                "web_backend.schedule_article_match_refresh",
                return_value={"scheduled": True, "reason": "cloud_upload_snapshot"},
            ),
            patch("web_backend.get_articles", return_value=copy.deepcopy(stale_articles)) as get_articles_mock,
            patch(
                "web_backend.refresh_article_matches",
                side_effect=AssertionError("deferred recovery must not sync refresh"),
            ) as refresh_mock,
            patch("web_backend.enqueue_cloud_articles") as article_enqueue_mock,
            patch.object(runtime, "_schedule_cloud_articles_snapshot_retry") as retry_mock,
        ):
            result = runtime._recover_cloud_run_history_uploads()

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("run_records"), {"run_queued": 2})
        self.assertEqual(result.get("articles_sync", {}).get("deferred"), True)
        self.assertEqual(result.get("articles_sync", {}).get("queued"), 0)
        run_enqueue_mock.assert_called_once()
        get_articles_mock.assert_called_once()
        refresh_mock.assert_not_called()
        article_enqueue_mock.assert_not_called()
        retry_mock.assert_called_once()
        self.assertEqual(fake_outbox.bound_session, _cloud_session())


if __name__ == "__main__":
    unittest.main()
