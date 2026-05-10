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

        self.assertEqual([item["title"] for item in result], ["cloud snapshot"])
        get_articles_mock.assert_called_once()
        refresh_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
