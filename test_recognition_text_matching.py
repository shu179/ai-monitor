#!/usr/bin/env python3

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import core.article_store as article_store
from core.recognition import ClipboardRecognitionManager


class RecognitionTextMatchingTests(unittest.TestCase):
    def test_global_recognition_mode_uses_active_task_brands_even_if_task_flag_is_false(self):
        config = {
            "detection_mode": "recognition",
            "tasks": [
                {
                    "name": "智源本珍",
                    "enabled": True,
                    "recognition_enabled": False,
                    "brand": "智源本珍",
                    "keywords": [
                        {
                            "keyword": "牛肉品牌推荐",
                            "brand": "智源本珍",
                            "platforms": ["doubao"],
                        }
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(lambda: config)

        matched = manager._match_brands_from_text("这篇内容里明确提到了智源本珍牛肉品牌。")

        self.assertIn("智源本珍", matched)

    def test_legacy_non_recognition_mode_still_supports_recognition_enabled_tasks(self):
        config = {
            "detection_mode": "browser",
            "tasks": [
                {
                    "name": "测试任务",
                    "enabled": True,
                    "recognition_enabled": True,
                    "recognition_brands": "DeepSeek, Kimi",
                    "guide_keywords": [
                        {"keyword": "测试", "brand": "文心一言"},
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(lambda: config)

        matched = manager._match_brands_from_text("DeepSeek 和文心一言都在这段文本里。")

        self.assertIn("DeepSeek", matched)
        self.assertIn("文心一言", matched)

    def test_text_mode_send_image_uses_source_text_hash_when_image_path_is_empty(self):
        manager = ClipboardRecognitionManager(lambda: {})
        with tempfile.TemporaryDirectory() as temp_dir:
            manager._save_dir = Path(temp_dir)

            def fake_render_text_to_screenshot(**kwargs):
                output_path = Path(kwargs["output_path"])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"fake image")
                return str(output_path)

            with patch("platforms.html_renderer.render_text_to_screenshot", side_effect=fake_render_text_to_screenshot):
                first = manager._build_text_mode_send_image(
                    image_path="",
                    source_text="众诚 文本 A",
                    platform_name="doubao",
                    brand="众诚",
                    keyword="武汉高端相亲平台",
                )
                second = manager._build_text_mode_send_image(
                    image_path="",
                    source_text="众诚 文本 B",
                    platform_name="doubao",
                    brand="众诚",
                    keyword="武汉高端相亲平台",
                )

        self.assertNotEqual(first, second)
        self.assertNotEqual(Path(first).name, "_doubao_dom.jpg")
        self.assertNotEqual(Path(second).name, "_doubao_dom.jpg")

    def test_dom_text_mode_auto_sends_even_when_ocr_toggle_is_off(self):
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "safe_mode_ocr_enabled": False,
                "dom_render_mode": True,
            },
            "tasks": [
                {
                    "name": "DOM自动发送任务",
                    "task_id": "unit_dom_auto_send_task",
                    "enabled": True,
                    "brand": "荟舒服",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 1,
                    "keywords": [
                        {
                            "keyword": "沙发品牌推荐",
                            "brand": "荟舒服",
                            "platforms": ["doubao"],
                        }
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(lambda: config)
        tasks = manager._get_enabled_tasks()

        manager._route_to_batches(
            image_path="",
            brands=["荟舒服"],
            summary="检测到品牌: 荟舒服",
            tasks=tasks,
            ocr_text="荟舒服 的回答文本",
        )

        self.assertEqual(manager._send_queue.qsize(), 1)
        self.assertEqual(len(manager._pending_batches), 0)

    def test_multi_platform_keyword_updates_use_one_image_per_platform(self):
        manager = ClipboardRecognitionManager(lambda: {})

        updates = manager._build_keyword_updates_from_batch(
            {
                "matched_pairs": [
                    {
                        "keyword": "成都考研集训营",
                        "brand": "启航考研",
                        "platforms": ["doubao", "deepseek"],
                    }
                ]
            },
            image_paths=["/tmp/doubao.jpg", "/tmp/deepseek.jpg"],
            detected_platforms=[],
        )

        self.assertEqual(
            [
                (item["keyword"], item["platform"], item["image_path"])
                for item in updates
            ],
            [
                ("成都考研集训营", "doubao", "/tmp/doubao.jpg"),
                ("成都考研集训营", "deepseek", "/tmp/deepseek.jpg"),
            ],
        )

    def test_dom_text_mode_uses_opened_platform_for_multi_platform_keyword(self):
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "safe_mode_ocr_enabled": False,
                "dom_render_mode": True,
            },
            "tasks": [
                {
                    "name": "启航考研",
                    "task_id": "unit_qihang_multi_platform",
                    "enabled": True,
                    "brand": "启航考研",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 2,
                    "keywords": [
                        {
                            "keyword": "成都考研集训营",
                            "brand": "启航考研",
                            "platforms": ["doubao", "deepseek"],
                        }
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(lambda: config)
        tasks = manager._get_enabled_tasks()

        manager.set_active_capture_platform("doubao")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="豆包回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )
        guide_items = manager._build_keyword_guide_items(tasks)
        self.assertEqual(len(guide_items), 1)
        self.assertEqual(guide_items[0]["platforms"], ["deepseek"])
        self.assertNotIn("豆包", guide_items[0]["detail_text"])

        manager.set_active_capture_platform("deepseek")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="DeepSeek 回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )

        batch = manager._send_queue.get_nowait()
        self.assertEqual(
            [
                (item["keyword"], item["platforms"])
                for item in batch["matched_pairs"]
            ],
            [
                ("成都考研集训营", ["doubao"]),
                ("成都考研集训营", ["deepseek"]),
            ],
        )

    def test_dom_text_mode_does_not_advance_keyword_until_all_platforms_complete(self):
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "safe_mode_ocr_enabled": False,
                "dom_render_mode": True,
            },
            "tasks": [
                {
                    "name": "启航考研",
                    "task_id": "unit_qihang_keyword_advance",
                    "enabled": True,
                    "brand": "启航考研",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 3,
                    "keywords": [
                        {
                            "keyword": "成都考研集训营",
                            "brand": "启航考研",
                            "platforms": ["doubao", "deepseek"],
                        },
                        {
                            "keyword": "考研辅导班推荐",
                            "brand": "启航考研",
                            "platforms": ["doubao"],
                        },
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(lambda: config)
        tasks = manager._get_enabled_tasks()

        manager.set_active_capture_platform("doubao")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="豆包回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )

        state_after_first_platform = manager.get_keyword_guide_state()
        self.assertEqual(state_after_first_platform["index"], 0)
        self.assertEqual(state_after_first_platform["items"][0]["keyword"], "成都考研集训营")
        self.assertEqual(state_after_first_platform["items"][0]["platforms"], ["deepseek"])

        manager.set_active_capture_platform("deepseek")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="DeepSeek 回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )

        state_after_all_platforms = manager.get_keyword_guide_state()
        self.assertEqual(state_after_all_platforms["index"], 0)
        self.assertEqual(state_after_all_platforms["items"][0]["keyword"], "考研辅导班推荐")
        self.assertEqual(state_after_all_platforms["items"][0]["platforms"], ["doubao"])

    def test_dom_text_mode_batch_ready_still_waits_for_remaining_platforms(self):
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "safe_mode_ocr_enabled": False,
                "dom_render_mode": True,
            },
            "tasks": [
                {
                    "name": "启航考研",
                    "task_id": "unit_qihang_batch_ready_waits_platforms",
                    "enabled": True,
                    "brand": "启航考研",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 1,
                    "keywords": [
                        {
                            "keyword": "成都考研集训营",
                            "brand": "启航考研",
                            "platforms": ["doubao", "deepseek"],
                        },
                        {
                            "keyword": "考研辅导班推荐",
                            "brand": "启航考研",
                            "platforms": ["doubao"],
                        },
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(lambda: config)
        tasks = manager._get_enabled_tasks()

        manager.set_active_capture_platform("doubao")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="豆包回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )

        state = manager.get_keyword_guide_state()
        self.assertEqual(state["index"], 0)
        self.assertEqual(state["items"][0]["keyword"], "成都考研集训营")
        self.assertEqual(state["items"][0]["platforms"], ["deepseek"])
        self.assertEqual(manager._send_queue.qsize(), 1)

    def test_multi_platform_keyword_without_platform_hint_does_not_advance(self):
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "safe_mode_ocr_enabled": False,
                "dom_render_mode": True,
            },
            "tasks": [
                {
                    "name": "启航考研",
                    "task_id": "unit_qihang_no_platform_hint",
                    "enabled": True,
                    "brand": "启航考研",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 3,
                    "keywords": [
                        {"keyword": "成都考研集训营", "brand": "启航考研", "platforms": ["doubao", "deepseek"]},
                        {"keyword": "考研辅导班推荐", "brand": "启航考研", "platforms": ["doubao"]},
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(lambda: config)
        tasks = manager._get_enabled_tasks()

        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="未携带平台的回答文本",
            platform_hint="",
        )

        state = manager.get_keyword_guide_state()
        self.assertEqual(state["index"], 0)
        self.assertEqual(state["items"][0]["keyword"], "成都考研集训营")
        self.assertEqual(state["items"][0]["platforms"], ["deepseek", "doubao"])

    def test_dom_text_batch_ready_prefers_same_brand_next_keyword_before_next_brand(self):
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "safe_mode_ocr_enabled": False,
                "dom_render_mode": True,
            },
            "tasks": [
                {
                    "name": "启航考研",
                    "task_id": "unit_qihang_same_brand_before_next_brand",
                    "enabled": True,
                    "brand": "启航考研",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 1,
                    "keywords": [
                        {"keyword": "成都考研集训营", "brand": "启航考研", "platforms": ["doubao"]},
                        {"keyword": "考研辅导班推荐", "brand": "启航考研", "platforms": ["doubao"]},
                    ],
                },
                {
                    "name": "另一个品牌",
                    "task_id": "unit_other_brand_after_qihang",
                    "enabled": True,
                    "brand": "另一个品牌",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 1,
                    "keywords": [
                        {"keyword": "另一个品牌推荐", "brand": "另一个品牌", "platforms": ["doubao"]},
                    ],
                },
            ],
        }
        manager = ClipboardRecognitionManager(lambda: config)
        tasks = manager._get_enabled_tasks()

        manager.set_active_capture_platform("doubao")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="豆包回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )

        state = manager.get_keyword_guide_state()
        self.assertEqual(state["index"], 0)
        self.assertEqual(state["items"][0]["task_name"], "启航考研")
        self.assertEqual(state["items"][0]["keyword"], "考研辅导班推荐")
        self.assertEqual(state["items"][1]["task_name"], "另一个品牌")

    def test_merged_pending_auto_send_notification_removes_completed_platforms(self):
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "safe_mode_ocr_enabled": False,
                "dom_render_mode": False,
            },
            "tasks": [
                {
                    "name": "启航考研",
                    "task_id": "unit_qihang_pending_merge_removes_platform",
                    "enabled": True,
                    "brand": "启航考研",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 1,
                    "keywords": [
                        {
                            "keyword": "成都考研集训营",
                            "brand": "启航考研",
                            "platforms": ["doubao", "deepseek", "kimi"],
                        },
                        {
                            "keyword": "考研辅导班推荐",
                            "brand": "启航考研",
                            "platforms": ["doubao"],
                        },
                    ],
                }
            ],
        }
        notified_states = []
        manager = ClipboardRecognitionManager(
            lambda: config,
            on_manual_state_change=lambda state: notified_states.append(state),
        )
        tasks = manager._get_enabled_tasks()

        manager.set_active_capture_platform("doubao")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="豆包回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )
        self.assertEqual(len(manager._pending_batches), 1)

        notified_states.clear()
        config["recognition"]["dom_render_mode"] = True
        manager.set_active_capture_platform("deepseek")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="DeepSeek 回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )

        self.assertTrue(notified_states)
        latest_state = notified_states[-1]
        self.assertEqual(latest_state["index"], 0)
        self.assertEqual(latest_state["items"][0]["keyword"], "成都考研集训营")
        self.assertEqual(latest_state["items"][0]["platforms"], ["kimi"])

    def test_merged_pending_auto_send_advances_after_all_platforms_complete(self):
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "safe_mode_ocr_enabled": False,
                "dom_render_mode": False,
            },
            "tasks": [
                {
                    "name": "启航考研",
                    "task_id": "unit_qihang_pending_merge_advances",
                    "enabled": True,
                    "brand": "启航考研",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 1,
                    "keywords": [
                        {"keyword": "成都考研集训营", "brand": "启航考研", "platforms": ["doubao", "deepseek"]},
                        {"keyword": "考研辅导班推荐", "brand": "启航考研", "platforms": ["doubao"]},
                    ],
                }
            ],
        }
        notified_states = []
        manager = ClipboardRecognitionManager(
            lambda: config,
            on_manual_state_change=lambda state: notified_states.append(state),
        )
        tasks = manager._get_enabled_tasks()

        manager.set_active_capture_platform("doubao")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="豆包回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )
        self.assertEqual(len(manager._pending_batches), 1)

        notified_states.clear()
        config["recognition"]["dom_render_mode"] = True
        manager.set_active_capture_platform("deepseek")
        manager._route_to_batches(
            image_path="",
            brands=["启航考研"],
            summary="检测到品牌: 启航考研",
            tasks=tasks,
            ocr_text="DeepSeek 回答文本",
            platform_hint=manager._get_active_capture_platform(),
        )

        self.assertTrue(notified_states)
        latest_state = notified_states[-1]
        self.assertEqual(latest_state["index"], 0)
        self.assertEqual(latest_state["items"][0]["keyword"], "考研辅导班推荐")
        self.assertEqual(latest_state["items"][0]["platforms"], ["doubao"])

    def test_article_title_matches_keyword_context_after_brand_removed(self):
        config = {
            "tasks": [
                {
                    "name": "万通",
                    "brand": "万通",
                    "keywords": [
                        {
                            "keyword": "万通职业教育",
                            "brand": "万通",
                            "platforms": ["doubao"],
                        }
                    ],
                }
            ],
        }

        analyzed = article_store.analyze_article_matches(
            "技能驱动未来：安徽职业教育的实践图景",
            config,
        )

        self.assertEqual(analyzed.get("matched_tasks"), ["万通"])
        self.assertIn("标题命中核心词", " ".join(analyzed.get("match_reasons", {}).get("万通", [])))

    def test_article_title_matches_provider_synonyms(self):
        config = {
            "tasks": [
                {
                    "name": "立体库品牌",
                    "brand": "立体库品牌",
                    "keywords": [
                        {
                            "keyword": "立体库厂家",
                            "brand": "立体库品牌",
                            "platforms": ["doubao"],
                        }
                    ],
                }
            ],
        }

        analyzed = article_store.analyze_article_matches(
            "立体库供应商推荐：五大主流厂商深度解析与选型指南",
            config,
        )

        self.assertEqual(analyzed.get("matched_tasks"), ["立体库品牌"])
        self.assertIn("标题命中关键词", " ".join(analyzed.get("match_reasons", {}).get("立体库品牌", [])))

    def test_article_title_matches_provider_keyword_subject(self):
        config = {
            "tasks": [
                {
                    "name": "立体库品牌",
                    "brand": "立体库品牌",
                    "keywords": [
                        {
                            "keyword": "立体库厂家",
                            "brand": "立体库品牌",
                            "platforms": ["doubao"],
                        }
                    ],
                }
            ],
        }

        analyzed = article_store.analyze_article_matches(
            "智能制造场景下立体库建设的关键趋势",
            config,
        )

        self.assertEqual(analyzed.get("matched_tasks"), ["立体库品牌"])
        self.assertIn("标题命中核心词", " ".join(analyzed.get("match_reasons", {}).get("立体库品牌", [])))

    def test_article_title_does_not_match_multiple_brands_by_shared_core_only(self):
        config = {
            "tasks": [
                {
                    "name": "立体库品牌A",
                    "brand": "品牌A",
                    "keywords": [
                        {
                            "keyword": "立体库厂家",
                            "brand": "品牌A",
                            "platforms": ["doubao"],
                        }
                    ],
                },
                {
                    "name": "立体库品牌B",
                    "brand": "品牌B",
                    "keywords": [
                        {
                            "keyword": "立体库厂家",
                            "brand": "品牌B",
                            "platforms": ["doubao"],
                        }
                    ],
                },
            ],
        }

        analyzed = article_store.analyze_article_matches(
            "智能制造场景下立体库建设的关键趋势",
            config,
        )

        self.assertEqual(analyzed.get("matched_tasks"), [])
        self.assertIn("缺少明确品牌信号", str(analyzed.get("unmatched_reason") or ""))

    def test_article_title_matches_task_by_region_and_industry_tags(self):
        config = {
            "tasks": [
                {
                    "name": "万通",
                    "brand": "万通",
                    "industry_tags": ["职业教育"],
                    "region_tags": ["安徽"],
                    "keywords": [
                        {
                            "keyword": "万通",
                            "brand": "万通",
                            "platforms": ["doubao"],
                        }
                    ],
                }
            ],
        }

        analyzed = article_store.analyze_article_matches(
            "技能驱动未来：安徽职业教育的实践图景",
            config,
        )

        self.assertEqual(analyzed.get("matched_tasks"), ["万通"])
        self.assertIn("标题命中行业/地区标签", " ".join(analyzed.get("match_reasons", {}).get("万通", [])))

    def test_copied_article_url_marks_current_task_article_as_referenced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_articles_file = article_store.ARTICLES_FILE
            original_overrides_file = article_store.DOMAIN_OVERRIDES_FILE
            original_media_names_file = article_store.DOMAIN_MEDIA_NAMES_FILE
            article_store.ARTICLES_FILE = Path(temp_dir) / "articles.json"
            article_store.DOMAIN_OVERRIDES_FILE = Path(temp_dir) / "domain_overrides.json"
            article_store.DOMAIN_MEDIA_NAMES_FILE = Path(temp_dir) / "domain_media_names.json"
            try:
                article_store.add_article({
                    "url": "https://example.com/a/123?utm_source=test",
                    "title": "品牌A 相关报道",
                    "media_name": "测试媒体",
                    "media_type": "authority",
                    "matched_tasks": ["品牌A任务"],
                    "match_reasons": {"品牌A任务": ["标题命中品牌名“品牌A”"]},
                })
                config = {
                    "detection_mode": "recognition",
                    "tasks": [
                        {
                            "name": "品牌A任务",
                            "enabled": True,
                            "brand": "品牌A",
                            "weekdays": [0, 1, 2, 3, 4, 5, 6],
                            "keywords": [
                                {
                                    "keyword": "品牌A 推荐",
                                    "brand": "品牌A",
                                    "platforms": ["doubao"],
                                }
                            ],
                        },
                        {
                            "name": "品牌B任务",
                            "enabled": True,
                            "brand": "品牌B",
                            "weekdays": [0, 1, 2, 3, 4, 5, 6],
                            "keywords": [
                                {
                                    "keyword": "品牌B 推荐",
                                    "brand": "品牌B",
                                    "platforms": ["doubao"],
                                }
                            ],
                        },
                    ],
                }
                manager = ClipboardRecognitionManager(lambda: config)
                manager.set_active_capture_platform("doubao")

                result = manager._mark_referenced_articles_from_text(
                    "引用链接：https://example.com/a/123?utm_campaign=abc",
                    tasks=manager._get_enabled_tasks(),
                    source="unit_test",
                )
                articles = article_store.get_articles()
            finally:
                article_store.ARTICLES_FILE = original_articles_file
                article_store.DOMAIN_OVERRIDES_FILE = original_overrides_file
                article_store.DOMAIN_MEDIA_NAMES_FILE = original_media_names_file

        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(articles[0].get("referenced_tasks"), ["品牌A任务"])
        hit = articles[0].get("reference_hits", {}).get("品牌A任务")
        self.assertIsInstance(hit, dict)
        self.assertEqual(hit.get("count"), 1)
        self.assertEqual(hit.get("source"), "unit_test")
        self.assertEqual(hit.get("events")[0].get("platform"), "doubao")
        self.assertEqual(hit.get("events")[0].get("source"), "unit_test")


if __name__ == "__main__":
    unittest.main()
