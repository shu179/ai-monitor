from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import core.article_store as article_store
from core.cloud_article_sync import cloud_articles_to_local_articles, pull_cloud_articles_into_store


class _FakeCloudSessionStore:
    def __init__(self) -> None:
        self.session = {
            "base_url": "https://api.surfacedlab.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "user": {"workspace_id": 1, "id": 2, "role": "operator"},
        }

    def load(self) -> dict:
        return dict(self.session)


class _PagedArticleClient:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.calls: list[dict] = []

    def task_articles(
        self,
        _access_token: str,
        *,
        updated_after: str = "",
        updated_after_id: int = 0,
        limit: int = 5000,
    ) -> dict:
        self.calls.append({
            "updated_after": updated_after,
            "updated_after_id": int(updated_after_id or 0),
            "limit": limit,
        })
        if not self.pages:
            return {"articles": [], "max_updated_at": updated_after, "max_article_id": updated_after_id}
        return self.pages.pop(0)


class CloudArticleSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
        }
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")

    def tearDown(self) -> None:
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        self._tmpdir.cleanup()

    def test_cloud_article_conversion_maps_visible_task_and_preserves_existing_id(self) -> None:
        existing = article_store.add_article(
            {
                "id": "local-existing",
                "url": "https://example.com/a",
                "title": "旧标题",
                "media_type": "selfmedia",
            }
        )
        config = {
            "tasks": [
                {"name": "即搜AI", "brand": "即搜AI", "cloud_task_id": 42},
                {"name": "另一个品牌", "cloud_task_id": 99},
            ]
        }

        articles = cloud_articles_to_local_articles(
            [
                {
                    "id": 7,
                    "canonical_url": "https://example.com/a",
                    "url_hash": "hash-a",
                    "title": "即搜AI 入选榜单",
                    "source": "武汉观察",
                    "media_type": "selfmedia",
                    "published_at": "2026-05-06T08:00:00+00:00",
                    "payload_json": {"excerpt": "摘要", "account_name": "账号"},
                    "created_at": "2026-05-06T08:10:00+00:00",
                    "updated_at": "2026-05-06T08:10:00+00:00",
                    "task_links": [
                        {"task_id": 42, "reason_json": {"reasons": ["标题匹配"]}},
                        {"task_id": 100, "reason_json": {"reasons": ["不可见"]}},
                    ],
                }
            ],
            config,
        )

        self.assertEqual(existing["id"], "local-existing")
        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["id"], "local-existing")
        self.assertEqual(article["matched_tasks"], ["即搜AI"])
        self.assertEqual(article["match_reasons"], {"即搜AI": ["标题匹配"]})
        self.assertEqual(article["cloud_article_id"], 7)
        self.assertEqual(article["cloud_task_ids"], [42])
        self.assertFalse(article["cloud_imported"])

    def test_prune_cloud_articles_keeps_cloud_articles_on_disk(self) -> None:
        article_store.import_article_store_bundle(
            {
                "articles": [
                    {
                        "id": "cloud-visible",
                        "url": "https://example.com/visible",
                        "title": "可见文章",
                        "fetch_method": "cloud",
                        "cloud_article_id": 1,
                        "cloud_task_ids": [42],
                    },
                    {
                        "id": "cloud-hidden",
                        "url": "https://example.com/hidden",
                        "title": "不可见文章",
                        "fetch_method": "cloud",
                        "cloud_article_id": 2,
                        "cloud_task_ids": [99],
                    },
                    {
                        "id": "local-manual",
                        "url": "https://example.com/manual",
                        "title": "本地文章",
                        "fetch_method": "html",
                        "matched_tasks": ["别的品牌"],
                    },
                ]
            },
            mode="replace",
        )

        result = article_store.prune_cloud_articles_by_visible_task_ids([42])

        ids = {item["id"] for item in article_store.get_articles()}
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["pruned_links"], 0)
        self.assertIn("cloud-visible", ids)
        self.assertIn("local-manual", ids)
        self.assertIn("cloud-hidden", ids)

    def test_pull_cloud_articles_paginates_and_hides_stale_cloud_links(self) -> None:
        article_store.import_article_store_bundle(
            {
                "articles": [
                    {
                        "id": "stale-cloud",
                        "url": "https://example.com/stale",
                        "title": "旧账号文章",
                        "fetch_method": "cloud",
                        "cloud_article_id": 3,
                        "cloud_task_ids": [42],
                        "matched_tasks": ["即搜AI"],
                        "match_reasons": {"即搜AI": ["历史归类"]},
                    },
                    {
                        "id": "legacy-stale-cloud",
                        "url": "https://example.com/legacy-stale",
                        "title": "旧版本云端文章",
                        "fetch_method": "cloud",
                        "cloud_article_id": 4,
                        "matched_tasks": ["即搜AI"],
                        "match_reasons": {"即搜AI": ["历史归类"]},
                    }
                ]
            },
            mode="replace",
        )
        config = {"tasks": [{"name": "即搜AI", "brand": "即搜AI", "cloud_task_id": 42}]}
        client = _PagedArticleClient([
            {
                "articles": [
                    {
                        "id": 7,
                        "canonical_url": "https://example.com/a",
                        "url_hash": "hash-a",
                        "title": "即搜AI 文章 A",
                        "source": "媒体",
                        "media_type": "selfmedia",
                        "updated_at": "2026-05-06T00:00:00+00:00",
                        "task_links": [{"task_id": 42}],
                    }
                ],
                "max_updated_at": "2026-05-06T00:00:00+00:00",
                "max_article_id": 7,
            },
            {
                "articles": [
                    {
                        "id": 8,
                        "canonical_url": "https://example.com/b",
                        "url_hash": "hash-b",
                        "title": "即搜AI 文章 B",
                        "source": "媒体",
                        "media_type": "selfmedia",
                        "updated_at": "2026-05-06T00:00:00+00:00",
                        "task_links": [{"task_id": 42}],
                    }
                ],
                "max_updated_at": "2026-05-06T00:00:00+00:00",
                "max_article_id": 8,
            },
            {"articles": [], "max_updated_at": "2026-05-06T00:00:00+00:00", "max_article_id": 8},
        ])

        summary = pull_cloud_articles_into_store(
            config,
            client=client,  # type: ignore[arg-type]
            session_store=_FakeCloudSessionStore(),
            force_full=True,
            limit=1,
        )

        articles = article_store.get_articles()
        urls = {item["url"] for item in articles}
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["fetched"], 2)
        self.assertEqual(summary["pruned"], 0)
        self.assertEqual(summary["pruned_links"], 2)
        self.assertEqual(summary["updated_after_id"], 8)
        self.assertEqual([call["updated_after_id"] for call in client.calls], [0, 7, 8])
        self.assertEqual(
            urls,
            {
                "https://example.com/stale",
                "https://example.com/legacy-stale",
                "https://example.com/a",
                "https://example.com/b",
            },
        )
        stale = next(item for item in articles if item["url"] == "https://example.com/stale")
        self.assertEqual(stale.get("cloud_task_ids"), [])
        self.assertEqual(stale.get("matched_tasks"), [])
        legacy_stale = next(item for item in articles if item["url"] == "https://example.com/legacy-stale")
        self.assertEqual(legacy_stale.get("cloud_task_ids"), [])
        self.assertEqual(legacy_stale.get("matched_tasks"), [])


if __name__ == "__main__":
    unittest.main()
