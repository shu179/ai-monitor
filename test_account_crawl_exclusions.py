import os
from pathlib import Path
import tempfile
import unittest

import core.account_crawler as account_crawler
import core.article_store as article_store
import core.sync_service as sync_service
from core.file_lock import CrossProcessRLock


class AccountCrawlExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
        os.environ["AIBRANDMONITOR_DATA_DIR"] = self._tmpdir.name

        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
            "ACCOUNT_CRAWL_STATE_FILE": account_crawler.ACCOUNT_CRAWL_STATE_FILE,
        }

        root = Path(self._tmpdir.name)
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        account_crawler.ACCOUNT_CRAWL_STATE_FILE = root / "logs" / "account_crawl_state.json"

    def tearDown(self) -> None:
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        account_crawler.ACCOUNT_CRAWL_STATE_FILE = self._original_paths["ACCOUNT_CRAWL_STATE_FILE"]
        if self._original_data_dir is None:
            os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
        else:
            os.environ["AIBRANDMONITOR_DATA_DIR"] = self._original_data_dir
        self._tmpdir.cleanup()

    def test_manual_delete_excludes_next_account_crawl_until_restored(self) -> None:
        url = "https://example.com/news/1?utm_campaign=abc"
        article = article_store.add_article({
            "url": url,
            "title": "要排除的文章",
            "media_name": "示例媒体",
        })

        removed = article_store.delete_article(str(article["id"]))
        self.assertIsNotNone(removed)
        self.assertTrue(article_store.is_article_url_excluded("https://example.com/news/1"))

        account = {
            "id": "account-a",
            "name": "账号 A",
            "url": "https://example.com",
            "platform": "rss",
            "platform_label": "RSS",
        }
        stored, status = account_crawler._store_article_item(
            {"url": "https://example.com/news/1", "title": "要排除的文章"},
            account,
            {},
            "unit",
        )
        self.assertIsNone(stored)
        self.assertEqual(status, "excluded")

        restore_result = article_store.restore_excluded_article_urls(["https://example.com/news/1"])
        self.assertEqual(restore_result["removed_count"], 1)

        stored, status = account_crawler._store_article_item(
            {"url": "https://example.com/news/1", "title": "要排除的文章"},
            account,
            {},
            "unit",
        )
        self.assertEqual(status, "added")
        self.assertIsNotNone(stored)

    def test_account_crawl_state_uses_cross_process_lock_file(self) -> None:
        self.assertIsInstance(account_crawler._lock, CrossProcessRLock)  # noqa: SLF001

        account_crawler._write_state({"accounts": {}})

        expected_lock_file = account_crawler.ACCOUNT_CRAWL_STATE_FILE.with_name(".account_crawl_state.json.lock")
        self.assertTrue(expected_lock_file.exists())

    def test_account_crawl_state_merge_preserves_other_accounts(self) -> None:
        account_crawler._write_state({
            "accounts": {
                "other-account": {
                    "last_status": "success",
                    "last_added_count": 3,
                }
            },
            "rsshub_success_bases": {"rss": "https://rsshub.old"},
        })
        account_crawler._merge_and_write_state_after_crawl(  # noqa: SLF001
            {
                "accounts": {
                    "current-account": {
                        "last_status": "error",
                        "last_added_count": 0,
                    }
                },
                "rsshub_success_bases": {"sohu": "https://rsshub.new"},
                "last_manual_run_at": "2026-05-09 10:00:00",
            },
            accounts=[{"id": "current-account"}],
            trigger="manual",
            summary={"excluded_count": 2, "excluded_links": [{"url": "https://example.com/a"}]},
        )

        stored = account_crawler._read_state()

        self.assertEqual(stored["accounts"]["other-account"]["last_added_count"], 3)
        self.assertEqual(stored["accounts"]["current-account"]["last_status"], "error")
        self.assertEqual(stored["rsshub_success_bases"]["rss"], "https://rsshub.old")
        self.assertEqual(stored["rsshub_success_bases"]["sohu"], "https://rsshub.new")
        self.assertEqual(stored["last_manual_run_at"], "2026-05-09 10:00:00")
        self.assertEqual(stored["last_excluded_count"], 2)

    def test_normalize_rsshub_base_urls_keeps_official_and_strips_legacy_fallbacks(self) -> None:
        bases = account_crawler._normalize_rsshub_base_urls(
            ["https://rsshub.akr.moe", "https://rsshub.example", "https://rsshub.app"],
            "https://rss.neoz.cc, https://rsshub.umzzz.com",
        )

        self.assertEqual(bases, ["https://rsshub.example", "https://rsshub.app"])

    def test_account_crawl_keeps_media_name_and_stores_account_name(self) -> None:
        account = {
            "id": "toutiao-a",
            "name": "增长超人",
            "url": "https://www.toutiao.com/c/user/token/MS4wLjABAAAA",
            "platform": "toutiao",
            "platform_label": "头条号",
        }

        stored, status = account_crawler._store_article_item(
            {"url": "https://toutiao.com/article/7605885920750912038", "title": "今日头条发布的文章"},
            account,
            {},
            "json",
        )

        self.assertEqual(status, "added")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.get("media_name"), "今日头条")
        self.assertEqual(stored.get("account_name"), "增长超人")
        self.assertEqual(article_store.resolve_article_export_source(stored), "今日头条（增长超人）")
        self.assertEqual(
            article_store.resolve_article_export_source(stored, show_selfmedia_account=False),
            "今日头条",
        )

    def test_cnblogs_account_crawl_uses_platform_media_name(self) -> None:
        account = {
            "id": "cnblogs-a",
            "name": "某个博客作者",
            "url": "https://www.cnblogs.com/example/",
            "platform": "cnblogs",
            "platform_label": "博客园",
        }

        stored, status = account_crawler._store_article_item(
            {"url": "https://www.cnblogs.com/example/p/123456.html", "title": "博客园文章"},
            account,
            {},
            "rss",
        )

        self.assertEqual(status, "added")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.get("media_name"), "博客园")
        self.assertEqual(stored.get("account_name"), "某个博客作者")
        self.assertEqual(article_store.resolve_article_export_source(stored), "博客园（某个博客作者）")

    def test_sohu_account_crawl_uses_platform_media_name_not_learned_account_name(self) -> None:
        article_store.DOMAIN_MEDIA_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.DOMAIN_MEDIA_NAMES_FILE.write_text(
            '{"sohu.com": "时尚潮流家"}',
            encoding="utf-8",
        )
        account = {
            "id": "sohu-a",
            "name": "品牌测评中心",
            "url": "https://mp.sohu.com/profile?xpt=120983381",
            "platform": "sohu",
            "platform_label": "搜狐号",
        }

        stored, status = account_crawler._store_article_item(
            {"url": "https://sohu.com/a/995634977_120983381", "title": "搜狐号文章"},
            account,
            {},
            "json",
        )

        self.assertEqual(status, "added")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.get("media_name"), "搜狐")
        self.assertEqual(stored.get("account_name"), "品牌测评中心")

    def test_sohu_uuid_profile_does_not_use_unsafe_direct_html_fallback(self) -> None:
        account = {
            "id": "sohu-uuid",
            "name": "品牌测评中心",
            "url": "https://mp.sohu.com/profile?xpt=NDExN2U2MTMtNzQ4ZS00NTI1LTkxODUtMTlmYzE1NWI1MTIy",
            "platform": "sohu",
            "platform_label": "搜狐号",
        }

        candidates = account_crawler._build_source_candidates(
            account,
            {"rsshub_base_urls": ["https://rsshub.example"], "max_items_per_account": 20},
        )
        html_articles = account_crawler._extract_articles_from_html(
            '<a href="https://sohu.com/a/1004481320_122598405">别的账号文章</a>',
            account["url"],
            "sohu",
            account=account,
        )

        self.assertFalse(any(item.get("url") == account["url"] and item.get("type") == "auto" for item in candidates))
        self.assertEqual(html_articles, [])
        self.assertEqual(account_crawler._normalize_sohu_author_id("NDExN2U2MTMtNzQ4ZS00NTI1LTkxODUtMTlmYzE1NWI1MTIy"), "")

    def test_toutiao_account_crawl_does_not_use_unverified_direct_html_fallback(self) -> None:
        account = {
            "id": "toutiao-a",
            "name": "品牌测评榜单",
            "url": "https://www.toutiao.com/c/user/token/MS4wLjABAAAAexample/",
            "platform": "toutiao",
            "platform_label": "头条号",
        }

        candidates = account_crawler._build_source_candidates(
            account,
            {"rsshub_base_urls": ["https://rsshub.example"], "max_items_per_account": 20},
        )

        self.assertFalse(any(item.get("url") == account["url"] and item.get("type") == "auto" for item in candidates))

    def test_account_crawl_duplicate_repairs_legacy_account_media_name(self) -> None:
        article_store.add_article({
            "url": "https://toutiao.com/article/7605885920750912039",
            "title": "旧记录",
            "media_name": "增长超人",
        })
        account = {
            "id": "toutiao-a",
            "name": "增长超人",
            "url": "https://www.toutiao.com/c/user/token/MS4wLjABAAAA",
            "platform": "toutiao",
            "platform_label": "头条号",
        }

        stored, status = account_crawler._store_article_item(
            {"url": "https://toutiao.com/article/7605885920750912039", "title": "旧记录"},
            account,
            {},
            "json",
        )

        self.assertIsNone(stored)
        self.assertEqual(status, "duplicate")
        updated = article_store.find_article_by_url("https://toutiao.com/article/7605885920750912039")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.get("media_name"), "今日头条")
        self.assertEqual(updated.get("account_name"), "增长超人")

    def test_account_crawl_duplicate_repairs_learned_same_platform_media_name(self) -> None:
        article_store.add_article({
            "url": "https://sohu.com/a/995634977_120983381",
            "title": "旧记录",
            "media_name": "时尚潮流家",
        })
        account = {
            "id": "sohu-a",
            "name": "品牌测评中心",
            "url": "https://mp.sohu.com/profile?xpt=120983381",
            "platform": "sohu",
            "platform_label": "搜狐号",
        }

        stored, status = account_crawler._store_article_item(
            {"url": "https://sohu.com/a/995634977_120983381", "title": "旧记录"},
            account,
            {},
            "json",
        )

        self.assertIsNone(stored)
        self.assertEqual(status, "duplicate")
        updated = article_store.find_article_by_url("https://sohu.com/a/995634977_120983381")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.get("media_name"), "搜狐")
        self.assertEqual(updated.get("account_name"), "品牌测评中心")

    def test_dedupe_sorts_articles_by_published_date_desc(self) -> None:
        items = [
            {"url": "https://example.com/1", "title": "旧文章", "published_at": "2024-01-01"},
            {"url": "https://example.com/2", "title": "新文章", "published_at": "2024-01-03"},
            {"url": "https://example.com/3", "title": "中间文章", "published_at": "2024-01-02"},
        ]

        deduped = account_crawler._dedupe_article_items(items)

        self.assertEqual([item["title"] for item in deduped], ["新文章", "中间文章", "旧文章"])

    def test_article_display_order_uses_published_date_not_append_order(self) -> None:
        article_store.add_article({
            "url": "https://example.com/news/new",
            "title": "新发布文章",
            "media_name": "示例媒体",
            "published_at": "2024-01-03",
            "ts": "2024-01-03",
        })
        article_store.add_article({
            "url": "https://example.com/news/old",
            "title": "旧发布文章",
            "media_name": "示例媒体",
            "published_at": "2024-01-01",
            "ts": "2024-01-01",
        })

        articles = article_store.get_articles()

        self.assertEqual([item["title"] for item in articles[:2]], ["新发布文章", "旧发布文章"])

    def test_toutiao_feed_cursor_uses_oldest_item_time(self) -> None:
        items = [
            {"title": "A", "behot_time": "1717200000"},
            {"title": "B", "behot_time": "1717000000"},
        ]

        cursor = account_crawler._extract_toutiao_feed_cursor({"data": items}, items)

        self.assertEqual(cursor, "1717000000")

    def test_toutiao_has_more_is_unknown_when_payload_omits_flag(self) -> None:
        self.assertIsNone(account_crawler._extract_toutiao_feed_has_more({"data": [{"title": "A"}]}))
        self.assertTrue(account_crawler._extract_toutiao_feed_has_more({"has_more": 1}))
        self.assertFalse(account_crawler._extract_toutiao_feed_has_more({"data": {"has_more": False}}))

    def test_sohu_json_parser_filters_to_account_and_keeps_public_time(self) -> None:
        account = {
            "id": "sohu-a",
            "name": "搜狐作者",
            "url": "https://mp.sohu.com/profile?xpt=120983381",
            "platform": "sohu",
            "platform_label": "搜狐号",
        }
        payload = {
            "data": {
                "pcArticleVOS": [
                    {
                        "id": 995634977,
                        "authorId": 120983381,
                        "title": "本账号新文章",
                        "publicTime": "1704153600000",
                    },
                    {
                        "id": 995634978,
                        "authorId": 999999999,
                        "title": "别的账号文章",
                        "publicTime": "2024-01-03 10:00:00",
                    },
                    {
                        "title": "搜狐号主页",
                        "url": "https://mp.sohu.com/profile?xpt=120983381",
                    },
                ],
            },
        }

        articles = account_crawler._extract_articles_from_json(
            payload,
            "https://v2.sohu.com/public-api/feed?scene=PROFILE&sceneId=120983381",
            "sohu",
            account=account,
        )

        self.assertEqual([item["title"] for item in articles], ["本账号新文章"])
        self.assertEqual(articles[0]["url"], "https://sohu.com/a/995634977_120983381")
        self.assertEqual(account_crawler._normalize_published_at(articles[0]["published_at"]), "2024-01-02")

    def test_sohu_html_fallback_filters_article_links_by_account_suffix(self) -> None:
        account = {
            "id": "sohu-a",
            "name": "搜狐作者",
            "url": "https://mp.sohu.com/profile?xpt=120983381",
            "platform": "sohu",
            "platform_label": "搜狐号",
        }
        html = """
        <a href="/a/995634977_120983381">本账号 HTML 文章</a>
        <a href="/a/995634978_999999999">别的账号 HTML 文章</a>
        <a href="https://mp.sohu.com/profile?xpt=120983381">搜狐号主页</a>
        <a href="/search?keyword=test">搜索页</a>
        """

        articles = account_crawler._extract_articles_from_html(
            html,
            "https://www.sohu.com/",
            "sohu",
            account=account,
        )

        self.assertEqual([item["title"] for item in articles], ["本账号 HTML 文章"])
        self.assertFalse(account_crawler._looks_like_article_url("sohu", "https://mp.sohu.com/profile?xpt=120983381"))

    def test_sohu_payload_parser_accepts_single_article_object(self) -> None:
        articles = account_crawler._parse_sohu_feed_items(
            account_crawler._extract_sohu_feed_payload_items({
                "data": {
                    "id": "995634977",
                    "authorId": "120983381",
                    "title": "单对象文章",
                    "publicTime": "2024-01-02 09:30:00",
                },
            }),
            account_xpt="120983381",
        )

        self.assertEqual([item["title"] for item in articles], ["单对象文章"])
        self.assertEqual(articles[0]["published_at"], "2024-01-02 09:30:00")

    def test_sohu_rsshub_fallback_filters_to_account_and_keeps_pubdate(self) -> None:
        account = {
            "id": "sohu-a",
            "name": "搜狐作者",
            "url": "https://mp.sohu.com/profile?xpt=120983381",
            "platform": "sohu",
            "platform_label": "搜狐号",
        }
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>RSSHub 本账号文章</title>
              <link>https://www.sohu.com/a/995634977_120983381</link>
              <pubDate>Tue, 02 Jan 2024 09:30:00 +0800</pubDate>
            </item>
            <item>
              <title>RSSHub 别的账号文章</title>
              <link>https://www.sohu.com/a/995634978_999999999</link>
              <pubDate>Wed, 03 Jan 2024 09:30:00 +0800</pubDate>
            </item>
            <item>
              <title>RSSHub 主页链接</title>
              <link>https://mp.sohu.com/profile?xpt=120983381</link>
              <pubDate>Thu, 04 Jan 2024 09:30:00 +0800</pubDate>
            </item>
          </channel>
        </rss>
        """

        articles = account_crawler._parse_rss_feed(
            xml,
            "https://rsshub.app/sohu/mp/120983381",
            "sohu",
            account=account,
        )

        self.assertEqual([item["title"] for item in articles], ["RSSHub 本账号文章"])
        self.assertEqual(account_crawler._normalize_published_at(articles[0]["published_at"]), "2024-01-02")

    def test_smzdm_source_candidates_include_rsshub_article_route(self) -> None:
        account = {
            "id": "smzdm-a",
            "name": "值得买作者",
            "url": "https://zhiyou.smzdm.com/member/6902738986/",
            "platform": "smzdm",
            "platform_label": "什么值得买",
        }

        candidates = account_crawler._build_source_candidates(
            account,
            {"rsshub_base_urls": ["https://rsshub.example"], "max_items_per_account": 20},
        )

        candidate_urls = [item["url"] for item in candidates]
        self.assertIn("https://rsshub.example/smzdm/article/6902738986", candidate_urls)
        self.assertIn("https://rsshub.app/smzdm/article/6902738986", candidate_urls)
        self.assertIn("https://zhiyou.smzdm.com/member/6902738986/", candidate_urls)
        self.assertTrue(any(item["type"] == "rss" for item in candidates))

    def test_smzdm_html_fallback_only_keeps_article_links(self) -> None:
        account = {
            "id": "smzdm-a",
            "name": "值得买作者",
            "url": "https://zhiyou.smzdm.com/member/6902738986/",
            "platform": "smzdm",
            "platform_label": "什么值得买",
        }
        html = """
        <a href="https://post.smzdm.com/p/aqzedvpx/">值得买文章</a>
        <a href="https://post.smzdm.com/zz/p/ardmqn0g/">值得买专栏文章</a>
        <a href="https://zhiyou.smzdm.com/member/6902738986/">个人主页</a>
        <a href="https://www.smzdm.com/tag/shuma/">标签页</a>
        """

        articles = account_crawler._extract_articles_from_html(
            html,
            "https://zhiyou.smzdm.com/member/6902738986/",
            "smzdm",
            account=account,
        )

        self.assertEqual(
            [item["url"] for item in articles],
            [
                "https://post.smzdm.com/p/aqzedvpx/",
                "https://post.smzdm.com/zz/p/ardmqn0g/",
            ],
        )

    def test_rss_parser_uses_url_guid_when_link_is_missing(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>博客园 GUID 链接文章</title>
              <guid>https://www.cnblogs.com/example/p/123456.html</guid>
              <pubDate>Tue, 02 Jan 2024 09:30:00 +0800</pubDate>
            </item>
          </channel>
        </rss>
        """

        articles = account_crawler._parse_rss_feed(
            xml,
            "https://www.cnblogs.com/example/rss",
            "cnblogs",
        )

        self.assertEqual([item["url"] for item in articles], ["https://www.cnblogs.com/example/p/123456.html"])

    def test_scoped_task_removal_keeps_article_for_other_brands(self) -> None:
        article = article_store.add_article({
            "url": "https://example.com/news/shared",
            "title": "品牌A 品牌B 共同报道",
            "media_name": "示例媒体",
            "matched_tasks": ["品牌A", "品牌B"],
            "match_reasons": {
                "品牌A": ["标题命中品牌名“品牌A”"],
                "品牌B": ["标题命中品牌名“品牌B”"],
            },
            "referenced_tasks": ["品牌A", "品牌B"],
            "reference_hits": {
                "品牌A": {"count": 1},
                "品牌B": {"count": 1},
            },
        })

        updated = article_store.remove_article_from_task(str(article["id"]), "品牌A")

        self.assertIsNotNone(updated)
        self.assertEqual(updated.get("matched_tasks"), ["品牌B"])
        self.assertEqual(updated.get("referenced_tasks"), ["品牌B"])
        self.assertNotIn("品牌A", updated.get("match_reasons") or {})
        self.assertNotIn("品牌A", updated.get("reference_hits") or {})
        self.assertIn("品牌A", updated.get("excluded_tasks") or [])
        self.assertFalse(article_store.is_article_url_excluded("https://example.com/news/shared"))

        refreshed = article_store.refresh_article_matches({
            "tasks": [
                {
                    "name": "品牌A",
                    "brand": "品牌A",
                    "keywords": [{"keyword": "品牌A 推荐", "brand": "品牌A"}],
                },
                {
                    "name": "品牌B",
                    "brand": "品牌B",
                    "keywords": [{"keyword": "品牌B 推荐", "brand": "品牌B"}],
                },
            ],
        })
        refreshed_article = next(item for item in refreshed if item.get("id") == article["id"])
        self.assertEqual(refreshed_article.get("matched_tasks"), ["品牌B"])
        self.assertIn("品牌A", refreshed_article.get("excluded_tasks") or [])

    def test_article_import_time_is_independent_from_published_date(self) -> None:
        article = article_store.add_article({
            "url": "https://example.com/news/old-published",
            "title": "旧发布日期的新录入文章",
            "media_name": "示例媒体",
            "published_at": "2024-01-02",
            "ts": "2024-01-02",
        })

        self.assertIn("imported_at", article)
        self.assertNotEqual(str(article.get("imported_at", ""))[:10], "2024-01-02")

    def test_sync_bundle_carries_excluded_article_urls(self) -> None:
        normalized_url = article_store.normalize_article_url("https://example.com/deleted?utm_source=x")
        article_store.exclude_article_url(
            "https://example.com/deleted?utm_source=x",
            title="已删除文章",
        )

        bundle = sync_service.build_sync_bundle({"tasks": []})
        article_memory = bundle["data"]["articleMemory"]
        self.assertIn(normalized_url, article_memory["excluded_article_urls"])
        self.assertEqual(bundle["stats"]["excludedArticleUrlCount"], 1)

        prepared = sync_service.prepare_sync_import({}, bundle, mode="merge")
        self.assertIn(normalized_url, prepared["article_bundle"]["excluded_article_urls"])


if __name__ == "__main__":
    unittest.main()
