"""验证 _mark_browser_extraction_references：抓取模式抓到的引用 URL → 调
mark_articles_referenced_by_urls，去重、空列表安全、传参正确。
"""
import sys
import types
import unittest


class BrowserExtractionReferencesMarkTests(unittest.TestCase):
    def setUp(self):
        # Mock article_store.mark_articles_referenced_by_urls，记录调用参数
        self._calls = []

        def fake_mark(task_names, urls, *, source, platform):
            self._calls.append({
                "task_names": list(task_names),
                "urls": list(urls),
                "source": source,
                "platform": platform,
            })
            return {"matched_count": min(len(urls), 1)}

        # 拦截 core.article_store 模块，注入 fake
        original_module = sys.modules.get("core.article_store")
        stub = types.ModuleType("core.article_store")
        stub.mark_articles_referenced_by_urls = fake_mark
        sys.modules["core.article_store"] = stub
        self.addCleanup(self._restore_module, "core.article_store", original_module)

    @staticmethod
    def _restore_module(name, original):
        if original is not None:
            sys.modules[name] = original
        else:
            sys.modules.pop(name, None)

    def _invoke(self, **kwargs):
        from core.task_executor_query import _mark_browser_extraction_references
        _mark_browser_extraction_references(**kwargs)

    def test_empty_task_name_skips(self):
        self._invoke(task_name="", platform_name="kimi", references=[{"url": "https://a"}], body_references=[])
        self.assertEqual(self._calls, [])

    def test_no_urls_skips(self):
        self._invoke(task_name="T", platform_name="kimi", references=[], body_references=[])
        self.assertEqual(self._calls, [])

    def test_dedupes_urls_across_references_and_body(self):
        self._invoke(
            task_name="任务A",
            platform_name="kimi",
            references=[
                {"url": "https://a.com/1"},
                {"url": "https://b.com/2"},
                {"url": ""},  # 空 url 跳过
                {"url": "https://a.com/1"},  # 重复
            ],
            body_references=[
                {"url": "https://b.com/2"},  # 跨数组重复
                {"url": "https://c.com/3"},
            ],
        )
        self.assertEqual(len(self._calls), 1)
        call = self._calls[0]
        self.assertEqual(call["task_names"], ["任务A"])
        self.assertEqual(call["urls"], ["https://a.com/1", "https://b.com/2", "https://c.com/3"])
        self.assertEqual(call["source"], "browser_extraction")
        self.assertEqual(call["platform"], "kimi")

    def test_silent_when_underlying_call_raises(self):
        # 让 mark_articles_referenced_by_urls 抛错，确认不向上传播
        def boom(*args, **kwargs):
            raise RuntimeError("simulated sqlite failure")
        sys.modules["core.article_store"].mark_articles_referenced_by_urls = boom
        try:
            self._invoke(
                task_name="T",
                platform_name="kimi",
                references=[{"url": "https://x"}],
                body_references=[],
            )
        except Exception as exc:
            self.fail(f"mark failure should not propagate: {exc}")


if __name__ == "__main__":
    unittest.main()
