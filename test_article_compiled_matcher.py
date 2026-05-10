import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import article_store


class CompiledArticleMatcherTests(unittest.TestCase):
    def test_compiled_matcher_matches_legacy_analyze_for_representative_articles(self) -> None:
        config = {
            "tasks": [
                {
                    "name": "万通",
                    "brand": "万通",
                    "industry_tags": ["职业教育"],
                    "region_tags": ["安徽"],
                    "keywords": [
                        {"keyword": "万通职业教育", "brand": "万通"},
                    ],
                },
                {
                    "name": "立体库品牌A",
                    "brand": "品牌A",
                    "keywords": [{"keyword": "立体库厂家", "brand": "品牌A"}],
                },
                {
                    "name": "立体库品牌B",
                    "brand": "品牌B",
                    "keywords": [{"keyword": "立体库厂家", "brand": "品牌B"}],
                },
            ],
        }
        cases = [
            (
                "技能驱动未来：安徽职业教育的实践图景",
                {},
                ["万通"],
            ),
            (
                "智能制造场景下立体库建设的关键趋势",
                {},
                [],
            ),
            (
                "智能制造场景下立体库建设的关键趋势",
                {"excerpt": "品牌A 在本轮方案中被重点提及"},
                ["立体库品牌A"],
            ),
            (
                "立体库供应商推荐：五大主流厂商深度解析与选型指南",
                {"source": "品牌B 官网"},
                ["立体库品牌B"],
            ),
        ]
        compiled = article_store.compile_article_matcher(config)

        for title, article, expected_tasks in cases:
            with self.subTest(title=title, expected_tasks=expected_tasks):
                legacy = article_store.analyze_article_matches(title, config, article=article)
                compiled_result = compiled.analyze(title, article=article)
                routed_result = article_store.analyze_article_matches(
                    title,
                    config,
                    article=article,
                    compiled_matcher=compiled,
                )

                self.assertEqual(compiled_result, legacy)
                self.assertEqual(routed_result, legacy)
                self.assertEqual(compiled_result.get("matched_tasks"), expected_tasks)

        tag_result = compiled.analyze("技能驱动未来：安徽职业教育的实践图景")
        self.assertIn("标题命中行业/地区标签", " ".join(tag_result.get("match_reasons", {}).get("万通", [])))
        shared_core = compiled.analyze("智能制造场景下立体库建设的关键趋势")
        self.assertIn("缺少明确品牌信号", str(shared_core.get("unmatched_reason") or ""))


class CompiledArticleMatcherRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_env = {
            article_store.ARTICLE_STORE_BACKEND_ENV: os.environ.get(article_store.ARTICLE_STORE_BACKEND_ENV),
            article_store.ARTICLE_SHADOW_WRITE_ENV: os.environ.get(article_store.ARTICLE_SHADOW_WRITE_ENV),
            "AIBRANDMONITOR_ARTICLE_STORE_SQLITE_MIGRATION_MIN_INTERVAL_SECONDS": os.environ.get(
                "AIBRANDMONITOR_ARTICLE_STORE_SQLITE_MIGRATION_MIN_INTERVAL_SECONDS"
            ),
        }
        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
            "ARTICLE_SHADOW_DB_FILE": article_store.ARTICLE_SHADOW_DB_FILE,
            "ARTICLE_STORE_DB_FILE": article_store.ARTICLE_STORE_DB_FILE,
        }
        os.environ[article_store.ARTICLE_SHADOW_WRITE_ENV] = "0"
        os.environ["AIBRANDMONITOR_ARTICLE_STORE_SQLITE_MIGRATION_MIN_INTERVAL_SECONDS"] = "0"

    def tearDown(self) -> None:
        article_store.wait_for_article_store_backend_migration(timeout=2.0)
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        article_store.ARTICLE_SHADOW_DB_FILE = self._original_paths["ARTICLE_SHADOW_DB_FILE"]
        article_store.ARTICLE_STORE_DB_FILE = self._original_paths["ARTICLE_STORE_DB_FILE"]
        article_store._small_document_cache.clear()  # noqa: SLF001
        article_store.reset_article_store_backend_health_for_tests()
        self._tmpdir.cleanup()

    def test_refresh_uses_compiled_matcher_and_keeps_json_sqlite_parity(self) -> None:
        json_result = self._run_refresh_with_compiled_assertion("json")
        sqlite_result = self._run_refresh_with_compiled_assertion("sqlite")

        self.assertEqual(sqlite_result, json_result)

    def test_excluded_tasks_affect_signature_and_refresh_output(self) -> None:
        config = {"tasks": [self._refresh_config()["tasks"][0]]}
        config_signature = article_store._article_match_config_signature(config)  # noqa: SLF001
        article = {
            "id": "article-a",
            "url": "https://example.com/a",
            "title": "Brand A launch coverage",
            "excluded_tasks": ["Brand A"],
        }
        fields = article_store._build_match_fields(article["title"], article)  # noqa: SLF001
        excluded_signature = article_store._article_match_signature(article, config_signature, fields=fields)  # noqa: SLF001
        included_article = {key: value for key, value in article.items() if key != "excluded_tasks"}
        included_fields = article_store._build_match_fields(included_article["title"], included_article)  # noqa: SLF001
        included_signature = article_store._article_match_signature(  # noqa: SLF001
            included_article,
            config_signature,
            fields=included_fields,
        )

        self.assertNotEqual(excluded_signature, included_signature)
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend):
                self._configure_paths(f"{backend}-excluded", backend)
                article_store.bulk_upsert_articles([article])
                refreshed = article_store.refresh_article_matches(config)
                self.assertEqual(refreshed[0].get("matched_tasks"), [])
                self.assertEqual(refreshed[0].get("excluded_tasks"), ["Brand A"])

    def test_json_warm_refresh_does_not_analyze_articles(self) -> None:
        self._configure_paths("json-warm", "json")
        config = self._refresh_config()
        article_store.bulk_upsert_articles(self._refresh_articles())
        article_store.refresh_article_matches(config)

        with patch.object(
            article_store,
            "analyze_article_matches",
            side_effect=AssertionError("warm refresh should not analyze"),
        ) as mocked:
            article_store.refresh_article_matches(config)

        self.assertEqual(mocked.call_count, 0)

    def test_json_small_dirty_refresh_only_analyzes_dirty_articles(self) -> None:
        self._configure_paths("json-small-dirty", "json")
        config = self._refresh_config()
        article_store.bulk_upsert_articles(self._refresh_articles())
        article_store.refresh_article_matches(config)
        article_store.update_article("article-b", {"title": "Brand B market dirty update"})
        original_analyze = article_store.analyze_article_matches

        with patch.object(article_store, "analyze_article_matches", wraps=original_analyze) as mocked:
            article_store.refresh_article_matches(config)

        self.assertEqual(mocked.call_count, 1)
        self.assertIsNotNone(mocked.call_args.kwargs.get("compiled_matcher"))

    def _run_refresh_with_compiled_assertion(self, backend: str) -> dict:
        self._configure_paths(f"{backend}-compiled-refresh", backend)
        config = self._refresh_config()
        article_store.bulk_upsert_articles(self._refresh_articles())
        original_analyze = article_store.analyze_article_matches

        with patch.object(article_store, "analyze_article_matches", wraps=original_analyze) as mocked:
            refreshed = article_store.refresh_article_matches(config)

        self.assertEqual(mocked.call_count, len(self._refresh_articles()))
        for call in mocked.call_args_list:
            self.assertIsNotNone(call.kwargs.get("compiled_matcher"))
            self.assertIsNotNone(call.kwargs.get("fields"))
        return {
            str(article.get("id")): {
                "matched_tasks": article.get("matched_tasks"),
                "match_reasons": article.get("match_reasons"),
                "unmatched_reason": article.get("unmatched_reason"),
                "_match_signature": article.get("_match_signature"),
            }
            for article in refreshed
        }

    def _configure_paths(self, label: str, backend: str) -> None:
        article_store.wait_for_article_store_backend_migration(timeout=2.0)
        article_store.reset_article_store_backend_health_for_tests()
        logs_dir = Path(self._tmpdir.name) / label / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE = logs_dir / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = logs_dir / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = logs_dir / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = logs_dir / "excluded_article_urls.json"
        article_store.ARTICLE_SHADOW_DB_FILE = logs_dir / "article_history_shadow.sqlite3"
        article_store.ARTICLE_STORE_DB_FILE = logs_dir / "article_store.sqlite3"
        article_store.ARTICLES_FILE.write_text(json.dumps([], ensure_ascii=False), encoding="utf-8")
        os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = backend
        article_store._small_document_cache.clear()  # noqa: SLF001

    def _refresh_config(self) -> dict:
        return {
            "tasks": [
                {
                    "name": "Brand A",
                    "brand": "Brand A",
                    "keywords": [{"keyword": "Brand A launch", "brand": "Brand A"}],
                },
                {
                    "name": "Brand B",
                    "brand": "Brand B",
                    "keywords": [{"keyword": "Brand B market", "brand": "Brand B"}],
                },
            ]
        }

    def _refresh_articles(self) -> list[dict]:
        return [
            {
                "id": "article-a",
                "url": "https://example.com/a",
                "title": "Brand A launch coverage",
                "published_at": "2026-05-09",
                "matched_tasks": ["Legacy"],
                "match_reasons": {"Legacy": ["seed"]},
            },
            {
                "id": "article-b",
                "url": "https://example.com/b",
                "title": "Brand B market update",
                "published_at": "2026-05-08",
                "matched_tasks": [],
            },
            {
                "id": "article-c",
                "url": "https://example.com/c",
                "title": "Unrelated article",
                "published_at": "2026-05-07",
                "matched_tasks": ["Brand A"],
                "excluded_tasks": ["Brand A"],
            },
        ]
