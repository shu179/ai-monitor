"""归类同步规则的测试：删关键词 / 删品牌应当让对应的自动归类自动取消，
重新加回完全相同的关键词 / 品牌后应当算回来；用户手动归类的文章永远保留。

测试聚焦 core.article_store._merge_task_classifications_for_refresh 的合并语义，
以及 refresh_article_matches 端到端在 JSON backend 下的真实行为。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import core.article_store as article_store


class MergeTaskClassificationsTests(unittest.TestCase):
    def test_inferred_hits_are_kept(self) -> None:
        merged, reasons = article_store._merge_task_classifications_for_refresh(
            stored=["品牌A"],
            inferred=["品牌A"],
            inferred_reasons={"品牌A": ["标题命中关键词“控油洗发水推荐”"]},
            existing_reasons={"品牌A": ["标题命中关键词“控油洗发水推荐”"]},
        )
        self.assertEqual(merged, ["品牌A"])
        self.assertEqual(reasons["品牌A"], ["标题命中关键词“控油洗发水推荐”"])

    def test_stored_keyword_hit_without_current_inferred_is_dropped(self) -> None:
        """旧版仅靠 stored 保留的「不飞粉」归类，新版默认取消归类。"""
        merged, reasons = article_store._merge_task_classifications_for_refresh(
            stored=["自然堂"],
            inferred=[],
            inferred_reasons={},
            existing_reasons={"自然堂": ["标题命中关键词“不飞粉”"]},
        )
        self.assertEqual(merged, [])
        self.assertEqual(reasons, {})

    def test_manual_classification_is_preserved_even_without_inferred(self) -> None:
        """用户在 UI 上显式归类的文章 — match_reasons 含"手动"二字 — 永远保留。"""
        merged, reasons = article_store._merge_task_classifications_for_refresh(
            stored=["自然堂"],
            inferred=[],
            inferred_reasons={},
            existing_reasons={"自然堂": ["手动设置所属品牌"]},
        )
        self.assertEqual(merged, ["自然堂"])
        self.assertEqual(reasons["自然堂"], ["手动设置所属品牌"])

    def test_stored_task_no_longer_in_config_is_dropped(self) -> None:
        """整个品牌被从 config 删除时，inferred 自然不会包含它，stored 也不再保留。"""
        merged, reasons = article_store._merge_task_classifications_for_refresh(
            stored=["旧品牌"],
            inferred=[],
            inferred_reasons={},
            existing_reasons={"旧品牌": ["标题命中关键词“某词”"]},
        )
        self.assertEqual(merged, [])
        self.assertEqual(reasons, {})


class RefreshArticleMatchesEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._original_backend = os.environ.get(article_store.ARTICLE_STORE_BACKEND_ENV)
        os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = "json"
        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
            "ARTICLE_STORE_DB_FILE": article_store.ARTICLE_STORE_DB_FILE,
        }
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        article_store.ARTICLE_STORE_DB_FILE = root / "logs" / "article_store.sqlite3"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._original_backend is None:
            os.environ.pop(article_store.ARTICLE_STORE_BACKEND_ENV, None)
        else:
            os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = self._original_backend
        for name, original in self._original_paths.items():
            setattr(article_store, name, original)
        self._tmpdir.cleanup()

    def _add_article(self, **fields: object) -> dict:
        return article_store.add_article(dict(fields))

    def test_deleting_a_keyword_drops_articles_that_were_only_hit_by_it(self) -> None:
        """先用关键词「不飞粉」让一篇文章归到「自然堂」，然后从 config 里删掉这个关键词，
        refresh 应当自动取消归类——下次加回完全相同的关键词时还能算回来。"""
        config_with_short_keyword = {
            "tasks": [
                {
                    "name": "自然堂",
                    "brand": "自然堂",
                    "keywords": [
                        {"keyword": "不飞粉"},
                        {"keyword": "控油洗发水推荐"},
                    ],
                }
            ]
        }
        # 标题命中 "不飞粉"，但跟"自然堂"品牌完全无关
        article = self._add_article(
            id="art-1",
            url="https://example.com/a",
            title="某不知名品牌的不飞粉粉饼测评",
            published_at="2026-05-26",
            ts="2026-05-26",
            media_type="selfmedia",
        )
        # 跑一次 refresh 让它被错归
        article_store.refresh_article_matches(config_with_short_keyword)
        stored = next(a for a in article_store.get_articles() if a["id"] == "art-1")
        self.assertIn("自然堂", stored.get("matched_tasks", []), msg="setup: 错归应当先发生")

        # 用户把"不飞粉"从 config 里删掉
        config_without_short_keyword = {
            "tasks": [
                {
                    "name": "自然堂",
                    "brand": "自然堂",
                    "keywords": [{"keyword": "控油洗发水推荐"}],
                }
            ]
        }
        article_store.refresh_article_matches(config_without_short_keyword)
        stored = next(a for a in article_store.get_articles() if a["id"] == "art-1")
        self.assertNotIn("自然堂", stored.get("matched_tasks", []), msg="删关键词后该归类应取消")
        self.assertEqual(stored.get("matched_tasks", []), [])

        # 重新把"不飞粉"加回来 → 应该算回去
        article_store.refresh_article_matches(config_with_short_keyword)
        stored = next(a for a in article_store.get_articles() if a["id"] == "art-1")
        self.assertIn("自然堂", stored.get("matched_tasks", []), msg="加回关键词后应当重新归类")

    def test_deleting_entire_brand_drops_all_its_classifications(self) -> None:
        """删掉整个 task → 归到该 task 的所有文章自动取消归类，再加回相同 task 时算回来。"""
        config = {
            "tasks": [
                {
                    "name": "测试品牌",
                    "brand": "测试品牌",
                    "keywords": [{"keyword": "测试品牌新品发布"}],
                }
            ]
        }
        self._add_article(
            id="art-2",
            url="https://example.com/b",
            title="测试品牌新品发布会现场记录",
            published_at="2026-05-26",
            ts="2026-05-26",
            media_type="selfmedia",
        )
        article_store.refresh_article_matches(config)
        stored = next(a for a in article_store.get_articles() if a["id"] == "art-2")
        self.assertIn("测试品牌", stored.get("matched_tasks", []))

        # 用户从 config 删掉整个品牌
        empty_config = {"tasks": []}
        article_store.refresh_article_matches(empty_config)
        stored = next(a for a in article_store.get_articles() if a["id"] == "art-2")
        self.assertEqual(stored.get("matched_tasks", []), [], msg="删品牌后归类应被清空")

        # 加回完全相同的品牌 → 应当重新归类
        article_store.refresh_article_matches(config)
        stored = next(a for a in article_store.get_articles() if a["id"] == "art-2")
        self.assertIn("测试品牌", stored.get("matched_tasks", []))

    def test_manual_classification_survives_config_changes(self) -> None:
        """用户在 UI 上手动归到某品牌的文章，即使 config 后续把那个关键词 / 品牌删了，
        归类也永远保留——因为 match_reasons 里写的是「手动设置所属品牌」。"""
        config = {
            "tasks": [
                {
                    "name": "测试品牌",
                    "brand": "测试品牌",
                    "keywords": [{"keyword": "测试品牌新品发布"}],
                }
            ]
        }
        self._add_article(
            id="art-3",
            url="https://example.com/c",
            title="完全不相关的标题",
            published_at="2026-05-26",
            ts="2026-05-26",
            media_type="selfmedia",
            matched_tasks=["测试品牌"],
            match_reasons={"测试品牌": ["手动设置所属品牌"]},
        )
        article_store.refresh_article_matches(config)
        stored = next(a for a in article_store.get_articles() if a["id"] == "art-3")
        self.assertEqual(stored.get("matched_tasks", []), ["测试品牌"], msg="手动归类应保留")

        # 删掉整个品牌
        article_store.refresh_article_matches({"tasks": []})
        stored = next(a for a in article_store.get_articles() if a["id"] == "art-3")
        self.assertEqual(stored.get("matched_tasks", []), ["测试品牌"], msg="手动归类即便删品牌也保留")


if __name__ == "__main__":
    unittest.main()
