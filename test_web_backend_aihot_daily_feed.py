import unittest
from datetime import date
from unittest.mock import patch

from web_backend import AppRuntime


class AihotDailyFeedTests(unittest.TestCase):
    def test_parse_aihot_daily_json_flattens_section_items(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        payload = runtime._parse_aihot_daily_json(
            {
                "date": "2026-05-26",
                "generatedAt": "2026-05-26T00:00:00.939Z",
                "sections": [
                    {
                        "label": "产品发布/更新",
                        "items": [
                            {
                                "title": "Grok Build Beta版向SuperGrok用户开放",
                                "summary": "Grok Build 已开放 Beta。",
                                "sourceUrl": "https://x.com/xai/status/1",
                                "sourceName": "X：xAI",
                            },
                            {
                                "title": "Qwen 隐式缓存功能上线",
                                "summary": "",
                                "sourceUrl": "https://x.com/Alibaba_Qwen/status/2",
                                "sourceName": "X：通义千问",
                            },
                        ],
                    },
                    {
                        "label": "行业动态",
                        "items": [
                            {
                                "title": "OpenAI 宣布内容合作",
                                "summary": "合作内容摘要",
                                "sourceUrl": "https://openai.com/example",
                                "sourceName": "OpenAI",
                            }
                        ],
                    },
                ],
            }
        )

        self.assertEqual(payload["feedUrl"], "https://aihot.virxact.com/api/public/daily")
        self.assertEqual([item["title"] for item in payload["items"]], [
            "Grok Build Beta版向SuperGrok用户开放",
            "Qwen 隐式缓存功能上线",
            "OpenAI 宣布内容合作",
        ])
        self.assertEqual(payload["items"][0]["author"], "X：xAI")
        self.assertEqual(payload["items"][2]["summary"], "合作内容摘要")

    def test_parse_aihot_daily_feed_keeps_only_today_items(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>AI Hot 日报</title>
    <item>
      <title>今天热点</title>
      <link>https://example.com/today</link>
      <pubDate>2026-05-26T08:00:00</pubDate>
      <description>今天内容</description>
    </item>
    <item>
      <title>昨天热点</title>
      <link>https://example.com/yesterday</link>
      <pubDate>2026-05-25T08:00:00</pubDate>
      <description>昨天内容</description>
    </item>
    <item>
      <title>前天热点</title>
      <link>https://example.com/older</link>
      <updated>2026-05-24T08:00:00</updated>
      <description>前天内容</description>
    </item>
  </channel>
</rss>"""

        with patch("web_backend.local_today", return_value=date(2026, 5, 26)):
            payload = runtime._parse_aihot_daily_feed(xml_text)

        self.assertEqual([item["title"] for item in payload["items"]], ["今天热点"])
        self.assertEqual(payload["items"][0]["summary"], "今天内容")


if __name__ == "__main__":
    unittest.main()
