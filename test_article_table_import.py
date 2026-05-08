import os
import json
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest

import core.article_store as article_store
from backend_lib.article_service import (
    _ARTICLE_IMPORT_BATCHES_LOCK,
    _article_import_batches_lock_file,
    _load_article_import_batches_file,
    _save_article_import_batches_file,
)
from core.file_lock import CrossProcessRLock
from web_backend import (
    ArticleImportBatchStore,
    ArticleService,
    _extract_article_import_items,
    _article_published_date,
    _normalize_article_import_media_type,
    _split_article_import_platform_account,
)


class _ArticleImportConfigProvider:
    def load(self):
        return {
            "tasks": [
                {
                    "name": "品牌A",
                    "brand": "品牌A",
                    "keywords": [{"keyword": "品牌A", "brand": "品牌A"}],
                }
            ]
        }


def _article_import_service(store: ArticleImportBatchStore | None = None) -> tuple[ArticleService, ArticleImportBatchStore]:
    batch_store = store or ArticleImportBatchStore()
    return (
        ArticleService(
            config_provider=_ArticleImportConfigProvider(),
            synced_articles_loader=lambda config: [],
            invalidate_article_cache=lambda: None,
            lock=threading.RLock(),
            import_batch_store=batch_store,
        ),
        batch_store,
    )


class ArticleTableImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
        os.environ["AIBRANDMONITOR_DATA_DIR"] = self._tmpdir.name
        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
        }
        root = Path(self._tmpdir.name)
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"

    def tearDown(self) -> None:
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        if self._original_data_dir is None:
            os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
        else:
            os.environ["AIBRANDMONITOR_DATA_DIR"] = self._original_data_dir
        self._tmpdir.cleanup()

    def _build_workbook(self) -> bytes:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "文章"
        worksheet.append(["文章标题", "文章链接", "来源媒体", "发布时间"])
        worksheet.append(["品牌A 发布新品", "https://example.com/news/a", "示例媒体", "2024-01-03"])
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_import_table_adds_articles_with_matches_and_can_undo(self) -> None:
        service, batch_store = _article_import_service()

        result = service.import_articles_from_file("文章导入.xlsx", self._build_workbook())

        self.assertTrue(result["ok"])
        self.assertEqual(result["added_count"], 1)
        import_id = result["import_id"]
        articles = article_store.get_articles()
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].get("media_name"), "示例媒体")
        self.assertIn("品牌A", articles[0].get("matched_tasks") or [])
        self.assertIn(import_id, batch_store.get_batches())

        undo_result = service.undo_article_import(import_id)

        self.assertTrue(undo_result["ok"])
        self.assertEqual(article_store.get_articles(), [])
        self.assertFalse(article_store.is_article_url_excluded("https://example.com/news/a"))

    def test_pending_import_batch_survives_runtime_reload(self) -> None:
        service, _ = _article_import_service()

        result = service.import_articles_from_file("文章导入.xlsx", self._build_workbook())
        self.assertTrue(result["ok"])

        reloaded_service, _ = _article_import_service()
        pending = reloaded_service.get_pending_article_imports()

        self.assertTrue(pending["ok"])
        self.assertEqual(len(pending["batches"]), 1)
        self.assertEqual(pending["batches"][0]["import_id"], result["import_id"])

    def test_import_batch_store_merge_uses_cross_process_lock(self) -> None:
        self.assertIsInstance(_ARTICLE_IMPORT_BATCHES_LOCK, CrossProcessRLock)
        _save_article_import_batches_file({
            "existing": {
                "id": "existing",
                "file_name": "existing.xlsx",
                "article_ids": ["article-a"],
                "status": "pending",
            }
        })

        store = ArticleImportBatchStore({
            "new": {
                "id": "new",
                "file_name": "new.xlsx",
                "article_ids": ["article-b"],
                "status": "pending",
            }
        })
        store.save(merge_existing=True)
        loaded = _load_article_import_batches_file()

        self.assertEqual(set(loaded), {"existing", "new"})
        self.assertTrue(_article_import_batches_lock_file().exists())

    def test_import_media_type_keeps_self_media(self) -> None:
        self.assertEqual(
            _normalize_article_import_media_type("自媒体", "https://example.com/a", "示例媒体"),
            "selfmedia",
        )

    def test_import_detects_side_by_side_article_tables(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "文章汇总"
        worksheet.append([
            "文章标题", "媒体名称", "发布时间", "发布链接", "",
            "文章标题", "自媒体名称", "发布时间", "发布链接",
        ])
        worksheet.append([
            "品牌A 权威报道", "示例媒体", "2024-01-03", "https://example.com/media-a", "",
            "品牌A 自媒体报道", "示例账号", "2024-01-04", "https://example.com/self-a",
        ])
        output = BytesIO()
        workbook.save(output)

        items, details = _extract_article_import_items("横排文章.xlsx", output.getvalue())

        self.assertEqual(len(items), 2)
        self.assertEqual([item["title"] for item in items], ["品牌A 权威报道", "品牌A 自媒体报道"])
        self.assertEqual(items[1]["account_name"], "示例账号")
        self.assertEqual(items[1]["media_type"], "selfmedia")
        self.assertEqual([sheet["count"] for sheet in details["sheets"]], [1, 1])

    def test_import_detects_reordered_columns_and_late_header(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "不规则"
        worksheet.append(["客户", "品牌A"])
        worksheet.append(["月份", "2024-01"])
        worksheet.append([])
        worksheet.append(["来源媒体", "发布日期", "文章标题", "原文链接"])
        worksheet.append(["示例媒体", "2024-01-05", "品牌A 不规则表头报道", "https://example.com/reordered"])
        output = BytesIO()
        workbook.save(output)

        items, details = _extract_article_import_items("不规则.xlsx", output.getvalue())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "品牌A 不规则表头报道")
        self.assertEqual(items[0]["media_name"], "示例媒体")
        self.assertEqual(items[0]["published_at"], "2024-01-05")
        self.assertEqual(details["sheets"][0]["count"], 1)

    def test_import_detects_vertical_article_tables(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "多表"
        worksheet.append(["文章标题", "媒体名称", "发布时间", "发布链接"])
        worksheet.append(["品牌A 上半区报道", "示例媒体", "2024-01-07", "https://example.com/top"])
        worksheet.append([])
        worksheet.append(["文章标题", "自媒体名称", "发布时间", "发布链接"])
        worksheet.append(["品牌A 下半区报道", "头条-示例账号", "2024-01-08", "https://www.toutiao.com/article/7615924630036955694/"])
        output = BytesIO()
        workbook.save(output)

        items, details = _extract_article_import_items("纵向多表.xlsx", output.getvalue())

        self.assertEqual([item["title"] for item in items], ["品牌A 上半区报道", "品牌A 下半区报道"])
        self.assertEqual(items[1]["account_name"], "头条-示例账号")
        self.assertEqual([sheet["count"] for sheet in details["sheets"]], [1, 1])

    def test_import_reads_generic_hyperlink_cells(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "文章链接", "来源媒体", "发布时间"])
        worksheet.append(["品牌A 超链接报道", "点击查看", "示例媒体", "2024-01-09"])
        worksheet["B2"].hyperlink = "https://example.com/hyperlink"
        output = BytesIO()
        workbook.save(output)

        items, _ = _extract_article_import_items("超链接.xlsx", output.getvalue())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.com/hyperlink")

    def test_import_uses_title_hyperlink_as_article_url(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "来源媒体", "发布时间"])
        worksheet.append(["品牌A 标题带链接报道", "示例媒体", "2024-01-09"])
        worksheet["A2"].hyperlink = "https://example.com/title-hyperlink"
        output = BytesIO()
        workbook.save(output)

        items, _ = _extract_article_import_items("标题超链接.xlsx", output.getvalue())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "品牌A 标题带链接报道")
        self.assertEqual(items[0]["url"], "https://example.com/title-hyperlink")

    def test_import_self_media_name_is_stored_as_account_name(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "自媒体名称", "发布时间", "发布链接"])
        worksheet.append([
            "品牌A 头条账号文章",
            "野渡泛舟客",
            "2024-01-06",
            "https://www.toutiao.com/article/7615924630036955694/",
        ])
        output = BytesIO()
        workbook.save(output)
        service, _ = _article_import_service()

        result = service.import_articles_from_file("自媒体文章.xlsx", output.getvalue())

        self.assertTrue(result["ok"])
        article = article_store.get_articles()[0]
        self.assertEqual(article.get("media_name"), "今日头条")
        self.assertEqual(article.get("account_name"), "野渡泛舟客")
        self.assertEqual(article.get("media_type"), "selfmedia")

    def test_import_toutiao_alias_uses_full_media_name(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "文章链接", "来源媒体", "发布时间"])
        worksheet.append([
            "品牌A 头条媒体名文章",
            "https://www.toutiao.com/article/7615924630036955694/",
            "头条",
            "2024-01-06",
        ])
        output = BytesIO()
        workbook.save(output)
        service, _ = _article_import_service()

        result = service.import_articles_from_file("头条别名.xlsx", output.getvalue())

        self.assertTrue(result["ok"])
        article = article_store.get_articles()[0]
        self.assertEqual(article.get("media_name"), "今日头条")

    def test_self_media_platform_domain_repairs_learned_account_name(self) -> None:
        article_store.DOMAIN_MEDIA_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.DOMAIN_MEDIA_NAMES_FILE.write_text(
            json.dumps({"sohu.com": "时尚潮流家"}, ensure_ascii=False),
            encoding="utf-8",
        )
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text(
            json.dumps(
                [
                    {
                        "id": "dirty-sohu",
                        "url": "https://sohu.com/a/995634977_120983381",
                        "title": "品牌A 搜狐文章",
                        "media_name": "时尚潮流家",
                        "media_type": "selfmedia",
                        "ts": "2024-01-06",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        article = article_store.get_articles()[0]
        stored = json.loads(article_store.ARTICLES_FILE.read_text(encoding="utf-8"))[0]

        self.assertEqual(article.get("media_name"), "搜狐")
        self.assertEqual(article_store.resolve_article_source(article), "搜狐")
        self.assertEqual(stored.get("media_name"), "搜狐")

        article_store.save_domain_media_name("sohu.com", "另一个搜狐账号", force=True)
        media_names = json.loads(article_store.DOMAIN_MEDIA_NAMES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(media_names.get("sohu.com"), "搜狐")

    def test_import_self_media_account_name_in_media_column_is_kept_as_account_name(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "媒体名称", "发布时间", "发布链接"])
        worksheet.append([
            "品牌A 搜狐账号文章",
            "品牌测评中心",
            "2024-01-06",
            "https://sohu.com/a/995634977_120983381",
        ])
        output = BytesIO()
        workbook.save(output)
        service, _ = _article_import_service()

        result = service.import_articles_from_file("搜狐账号列.xlsx", output.getvalue())

        self.assertTrue(result["ok"])
        article = article_store.get_articles()[0]
        self.assertEqual(article.get("media_name"), "搜狐")
        self.assertEqual(article.get("account_name"), "品牌测评中心")

    def test_import_without_publish_date_stays_undated(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "文章链接", "来源媒体"])
        worksheet.append(["品牌A 缺少日期报道", "https://example.com/no-date", "示例媒体"])
        output = BytesIO()
        workbook.save(output)
        service, _ = _article_import_service()

        result = service.import_articles_from_file("缺日期.xlsx", output.getvalue())

        self.assertTrue(result["ok"])
        article = article_store.get_articles()[0]
        self.assertEqual(article.get("published_at"), "")
        self.assertEqual(article.get("ts"), "")
        self.assertIsNone(_article_published_date(article))

    def test_import_existing_url_updates_metadata_and_undo_restores(self) -> None:
        original = article_store.add_article({
            "url": "https://example.com/existing",
            "title": "旧标题",
            "media_name": "旧媒体",
            "media_type": "authority",
            "published_at": "2024-01-01",
            "ts": "2024-01-01",
            "matched_tasks": [],
            "match_reasons": {},
            "unmatched_reason": "",
        })
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "文章链接", "来源媒体", "发布时间"])
        worksheet.append(["品牌A 更新后的标题", "https://example.com/existing", "新媒体", "2024-01-10"])
        output = BytesIO()
        workbook.save(output)
        service, _ = _article_import_service()

        result = service.import_articles_from_file("品牌A更新.xlsx", output.getvalue())

        self.assertTrue(result["ok"])
        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["updated_count"], 1)
        updated = article_store.find_article_by_url("https://example.com/existing")
        self.assertEqual(updated.get("title"), "品牌A 更新后的标题")
        self.assertEqual(updated.get("published_at"), "2024-01-10")
        self.assertIn("品牌A", updated.get("matched_tasks") or [])

        undo_result = service.undo_article_import(result["import_id"])

        self.assertTrue(undo_result["ok"])
        restored = article_store.find_article_by_url("https://example.com/existing")
        self.assertEqual(restored.get("id"), original.get("id"))
        self.assertEqual(restored.get("title"), "旧标题")
        self.assertEqual(restored.get("media_name"), "旧媒体")
        self.assertEqual(restored.get("published_at"), "2024-01-01")

    def test_import_splits_platform_account_text(self) -> None:
        self.assertEqual(_split_article_import_platform_account("头条（野渡泛舟客）"), ("今日头条", "野渡泛舟客"))
        self.assertEqual(_split_article_import_platform_account("头条-野渡泛舟客"), ("今日头条", "野渡泛舟客"))
        self.assertEqual(_split_article_import_platform_account("搜狐号/品牌测评榜单"), ("搜狐号", "品牌测评榜单"))
        self.assertEqual(_split_article_import_platform_account("36氪-出海"), ("", ""))

    def test_import_account_column_can_include_platform_account_text(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "自媒体名称", "发布时间", "发布链接"])
        worksheet.append([
            "品牌A 组合账号文章",
            "头条（野渡泛舟客）",
            "2024-01-06",
            "https://www.toutiao.com/article/7615924630036955694/",
        ])
        output = BytesIO()
        workbook.save(output)
        service, _ = _article_import_service()

        result = service.import_articles_from_file("组合账号.xlsx", output.getvalue())

        self.assertTrue(result["ok"])
        article = article_store.get_articles()[0]
        self.assertEqual(article.get("media_name"), "今日头条")
        self.assertEqual(article.get("account_name"), "野渡泛舟客")

    def test_import_uses_file_name_as_classification_hint(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["文章标题", "文章链接", "来源媒体", "发布时间"])
        worksheet.append(["没有品牌词的行业报道", "https://example.com/file-hint", "示例媒体", "2024-01-11"])
        output = BytesIO()
        workbook.save(output)
        service, _ = _article_import_service()

        result = service.import_articles_from_file("GEO-品牌A-3月汇总.xlsx", output.getvalue())

        self.assertTrue(result["ok"])
        article = article_store.get_articles()[0]
        self.assertIn("品牌A", article.get("matched_tasks") or [])


if __name__ == "__main__":
    unittest.main()
