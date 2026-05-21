import json
import os
import unittest

from platforms.tongyi import TongyiPlatform


# 深度思考模式 SSE event：嵌套 multi_load/iframe → source_group_web → source
_FIXTURE_SOURCE_EVENT = (
    '{"data":{"messages":[{"mime_type":"multi_load/iframe","meta_data":'
    '{"multi_load":[{"type":"source_group_web","content":{"list":['
    '{"type":"source","content":{"list":['
    '{"title":"T1","url":"https://example.com/1","name":"S1"},'
    '{"title":"T2","url":"https://example.com/2","name":"S2"}'
    ']}}'
    ']}}]}}]}}'
)

# 非深度思考模式 SSE event：平铺 bar/progress.meta_data.list[]，type=web_source
_FIXTURE_BAR_PROGRESS_EVENT = (
    '{"data":{"messages":[{"mime_type":"bar/progress","meta_data":'
    '{"match_num":2,"list":['
    '{"type":"web_source","title":"B1","url":"https://bar.example.com/1","name":"BS1"},'
    '{"type":"web_source","title":"B2","url":"https://bar.example.com/2","name":"BS2"}'
    ']}}]}}'
)


class TongyiPlatformTests(unittest.TestCase):
    def test_deep_think_selector_targets_current_qianwen_button(self):
        selector = TongyiPlatform.deep_think_selector
        self.assertIn('data-input-capsule-login-gate^="deep-think"', selector)
        self.assertIn('button[aria-label="思考"][aria-pressed]', selector)
        self.assertIn('qwpcicon-deepThinking', selector)

    def test_think_content_selector_covers_new_qianwen_markers(self):
        selector = TongyiPlatform.think_content_selector
        self.assertIn("reasoning", selector)
        self.assertIn("data-testid*=\"think\"", selector)
        self.assertIn("deepThink", selector)
        self.assertIn("thinkingWrap", selector)
        self.assertIn("thinkingTitle", selector)
        self.assertIn("thinkingHeader", selector)
        self.assertIn("data-card_name=\"deep_think\"", selector)

    def test_text_stable_completion_requires_complete_non_thinking_answer(self):
        platform = TongyiPlatform("/tmp/tongyi-test")
        self.assertFalse(platform._allow_text_stable_completion({}))
        self.assertFalse(platform._allow_text_stable_completion({"send_ready": True}))
        self.assertFalse(
            platform._allow_text_stable_completion(
                {"last_block_complete": True, "active_thinking": True}
            )
        )
        self.assertFalse(
            platform._allow_text_stable_completion(
                {"last_block_complete": True, "active_printing": True}
            )
        )
        self.assertFalse(
            platform._allow_text_stable_completion(
                {"last_block_complete": True, "stop_visible": True}
            )
        )
        self.assertTrue(
            platform._allow_text_stable_completion(
                {"last_block_complete": True, "active_thinking": False, "stop_visible": False}
            )
        )

    # ---- SSE reference 直抓相关测试 ----

    def test_parse_references_from_sse_basic(self):
        body = f"data:{_FIXTURE_SOURCE_EVENT}\n\n"
        refs = TongyiPlatform._parse_references_from_sse(body)
        self.assertEqual(len(refs), 2)
        self.assertEqual([r["index"] for r in refs], [1, 2])
        self.assertEqual(refs[0]["url"], "https://example.com/1")
        self.assertEqual(refs[0]["source"], "S1")
        self.assertEqual(refs[1]["title"], "T2")

    def test_parse_references_from_sse_bar_progress_mode(self):
        """非深度思考模式：bar/progress.meta_data.list[] 平铺结构。"""
        body = f"data:{_FIXTURE_BAR_PROGRESS_EVENT}\n\n"
        refs = TongyiPlatform._parse_references_from_sse(body)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["url"], "https://bar.example.com/1")
        self.assertEqual(refs[0]["source"], "BS1")
        self.assertEqual(refs[1]["title"], "B2")

    def test_parse_references_from_sse_mixed_modes_dedup(self):
        """两种结构混合时按 url 去重，互不污染。"""
        body = (
            f"data:{_FIXTURE_SOURCE_EVENT}\n\n"
            f"data:{_FIXTURE_BAR_PROGRESS_EVENT}\n\n"
        )
        refs = TongyiPlatform._parse_references_from_sse(body)
        self.assertEqual(len(refs), 4)
        urls = [r["url"] for r in refs]
        self.assertIn("https://example.com/1", urls)
        self.assertIn("https://bar.example.com/2", urls)

    def test_parse_references_from_sse_empty_and_done(self):
        self.assertEqual(TongyiPlatform._parse_references_from_sse(""), [])
        self.assertEqual(TongyiPlatform._parse_references_from_sse("data:[DONE]\n\n"), [])

    def test_parse_references_from_sse_skips_truncated_chunks(self):
        # 第一个 event 正常，第二个被截断的 JSON 应跳过且不污染前面的结果
        body = f"data:{_FIXTURE_SOURCE_EVENT}\n\ndata:{{broken_json"
        refs = TongyiPlatform._parse_references_from_sse(body)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["url"], "https://example.com/1")

    def test_parse_references_from_sse_dedups_by_url(self):
        # 同一 URL 在多个 event/group 出现，只保留首次
        body = (
            f"data:{_FIXTURE_SOURCE_EVENT}\n\n"
            f"data:{_FIXTURE_SOURCE_EVENT}\n\n"
        )
        refs = TongyiPlatform._parse_references_from_sse(body)
        self.assertEqual(len(refs), 2)

    @unittest.skipUnless(
        os.path.exists("/Users/shuao/Desktop/ai-monitor/logs/dump_tongyi_responses.jsonl"),
        "需要先跑 dump_tongyi_api.py 生成真实 SSE 样本（深度思考模式）",
    )
    def test_parse_references_from_real_dump_deep_think(self):
        """端到端：深度思考模式的真实 SSE dump (multi_load/iframe 嵌套结构)。"""
        with open("/Users/shuao/Desktop/ai-monitor/logs/dump_tongyi_responses.jsonl") as f:
            sse_body = None
            for line in f:
                rec = json.loads(line)
                if "event-stream" in str(rec.get("mime_type", "")).lower():
                    sse_body = rec.get("body_preview", "")
                    break
        self.assertTrue(sse_body, "dump 文件里没找到 SSE 响应")
        refs = TongyiPlatform._parse_references_from_sse(sse_body)
        self.assertGreaterEqual(len(refs), 1, "深度思考模式真实 SSE 至少应解析出 1 条引用")
        for r in refs:
            self.assertTrue(r["url"].startswith(("http://", "https://")))
            self.assertTrue(r["source"], f"引用 #{r['index']} 缺 source 名")
            self.assertTrue(r["title"], f"引用 #{r['index']} 缺 title")

    def test_extract_answer_references_prefers_sse_cache(self):
        """SSE 缓存有数据时直接解析，不调用点击路径。"""
        p = TongyiPlatform("/tmp/tongyi-test")
        p._chat_sse_body = f"data:{_FIXTURE_SOURCE_EVENT}\n\n"

        click_calls = [0]
        def fail_if_called():
            click_calls[0] += 1
            return [{"index": 999, "title": "from_click", "url": "x", "source": "x"}]
        p._extract_answer_references_via_click = fail_if_called

        refs = p.extract_answer_references()
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["url"], "https://example.com/1")
        self.assertEqual(click_calls[0], 0, "SSE 命中时不应回退到点击路径")

    def test_extract_answer_references_falls_back_when_cache_empty(self):
        """SSE 缓存为空（CDP 失效/未触发搜索）时回退到点击路径。"""
        p = TongyiPlatform("/tmp/tongyi-test")
        self.assertEqual(p._chat_sse_body, "")

        sentinel = [{"index": 1, "title": "fallback", "url": "https://x", "source": "click"}]
        p._extract_answer_references_via_click = lambda: sentinel

        refs = p.extract_answer_references()
        self.assertEqual(refs, sentinel)

    def test_extract_answer_references_falls_back_when_sse_has_no_refs(self):
        """SSE 缓存有 body 但不含 source_group_web（普通问候）时也应回退。"""
        p = TongyiPlatform("/tmp/tongyi-test")
        p._chat_sse_body = 'data:{"data":{"messages":[{"mime_type":"text","content":"你好"}]}}\n\n'
        sentinel = [{"index": 1, "title": "fallback", "url": "https://x", "source": "click"}]
        p._extract_answer_references_via_click = lambda: sentinel

        refs = p.extract_answer_references()
        self.assertEqual(refs, sentinel)


if __name__ == "__main__":
    unittest.main()
