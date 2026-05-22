from unittest.mock import patch

import pytest

from core.notification_idempotency import build_payload_hash
from core.recognition import ClipboardRecognitionManager
from core.time_utils import local_today


@pytest.fixture()
def manager(tmp_path):
    instance = object.__new__(ClipboardRecognitionManager)
    instance._save_dir = tmp_path
    return instance


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("豆包", "doubao"),
        ("DeepSeek", "deepseek"),
        ("Kimi", "kimi"),
        ("元宝", "yuanbao"),
        ("通义千问", "tongyi"),
        ("文心一言", "wenxin"),
        ("ChatGPT", "chatgpt"),
        ("Claude", "claude"),
        ("Gemini", "gemini"),
        ("Perplexity", "perplexity"),
    ],
)
def test_platform_aliases_normalize_existing_ids(manager, raw, expected):
    assert manager._normalize_platform_id(raw) == expected


@pytest.mark.parametrize(
    ("text", "candidates", "expected"),
    [
        ("", ["doubao"], ""),
        (None, ["doubao"], ""),
        ("豆包 发送消息或输入/选择技能", ["doubao", "deepseek"], "doubao"),
        ("腾讯元宝 有问题，尽管问", ["yuanbao", "doubao"], "yuanbao"),
        ("通义千问 向千问提问", ["tongyi", "wenxin"], "tongyi"),
        ("文心一言 自动适配需求", ["wenxin", "tongyi"], "wenxin"),
        ("deepseek 智能搜索", ["deepseek"], ""),
        ("kimi", ["kimi"], "kimi"),
        ("ChatGPT", ["chatgpt"], "chatgpt"),
        ("Claude by Anthropic", ["claude"], "claude"),
        ("Gemini Google AI", ["gemini"], "gemini"),
        ("Perplexity", ["perplexity"], "perplexity"),
    ],
)
def test_infer_platform_from_text_characterizes_tokens(manager, text, candidates, expected):
    assert manager._infer_platform_from_text(text, candidates) == expected


def test_infer_platform_from_text_characterizes_multi_platform_conflicts(manager):
    assert manager._infer_platform_from_text("kimi deepseek", ["deepseek", "kimi"]) == "kimi"
    assert manager._infer_platform_from_text("ChatGPT Claude", ["chatgpt", "claude"]) == "chatgpt"
    assert manager._infer_platform_from_text("Claude Gemini", ["claude", "gemini"]) == ""
    assert manager._infer_platform_from_text("给 deepseek 发送消息 Kimi", ["kimi", "deepseek"]) == "kimi"
    assert manager._infer_platform_from_text("给 deepseek 发送消息 Kimi", ["deepseek", "kimi"]) == "deepseek"


def test_infer_platform_from_body_text_uses_stricter_threshold_than_bottom_text(manager):
    assert manager._infer_platform_from_text("豆包 选择技能", ["doubao"]) == "doubao"
    assert manager._infer_platform_from_body_text("豆包 选择技能", ["doubao"]) == ""
    assert manager._infer_platform_from_body_text("ChatGPT", ["chatgpt"]) == "chatgpt"
    assert manager._infer_platform_from_body_text("ChatGPT Claude", ["chatgpt", "claude"]) == "chatgpt"
    assert manager._infer_platform_from_body_text("Claude Gemini", ["claude", "gemini"]) == ""
    assert manager._infer_platform_from_body_text("kimi deepseek", ["deepseek", "kimi"]) == "kimi"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", False),
        (None, False),
        ("发送消息或输入 / 选择技能", True),
        ("深度思考 联网搜索", True),
        ("ChatGPT 对话窗口", True),
        ("附件 上传", True),
        ("这是一段问题回答", True),
        ("品牌A正常回答内容", False),
    ],
)
def test_should_trim_input_bubble_characterizes_text_tokens(manager, text, expected):
    assert manager._should_trim_input_bubble(text) is expected


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        (None, 0),
        ({}, 0),
        ({"image_paths": []}, 0),
        ({"image_paths": ["/tmp/a.jpg", "/tmp/b.jpg"]}, 2),
        ({"image_items": [{"path": ""}, {"path": "/tmp/b.jpg"}]}, 2),
        ({"image_items": [], "image_paths": ["/tmp/fallback.jpg"]}, 0),
    ],
)
def test_batch_image_count_characterizes_payload_shapes(manager, batch, expected):
    assert manager._batch_image_count(batch) == expected


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        ({}, "累计 0 张（本次 0 张，历史 0 张）"),
        ({"image_paths": []}, "累计 0 张（本次 0 张，历史 0 张）"),
        ({"image_paths": ["/tmp/current.jpg"]}, "累计 1 张（本次 1 张，历史 0 张）"),
        (
            {
                "image_paths": ["/tmp/historical.jpg", "/tmp/current.jpg"],
                "current_image_count": 1,
                "historical_screenshot_count": 1,
                "total_image_count": 2,
            },
            "累计 2 张（本次 1 张，历史 1 张）",
        ),
        (
            {
                "image_paths": ["/tmp/a.jpg", "/tmp/b.jpg", "/tmp/c.jpg"],
                "current_image_count": 2,
                "historical_screenshot_count": 1,
                "total_image_count": 3,
            },
            "累计 3 张（本次 2 张，历史 1 张）",
        ),
    ],
)
def test_format_batch_image_progress_characterizes_counts(manager, batch, expected):
    assert manager._format_batch_image_progress(batch) == expected


@pytest.mark.parametrize(
    ("current_count", "batch_size", "historical_count", "expected"),
    [
        (0, 1, 0, "累计 0/1 张（本次 0 张，历史 0 张）"),
        (2, 3, 1, "累计 3/3 张（本次 2 张，历史 1 张）"),
        (-2, 0, -1, "累计 0/1 张（本次 0 张，历史 0 张）"),
        ("2", "5", "1", "累计 3/5 张（本次 2 张，历史 1 张）"),
    ],
)
def test_format_threshold_progress_text_characterizes_clamping(
    manager,
    current_count,
    batch_size,
    historical_count,
    expected,
):
    assert manager._format_threshold_progress_text(current_count, batch_size, historical_count) == expected


def test_keyword_state_complete_for_platforms_characterizes_platform_state_shapes(manager):
    assert manager._keyword_state_complete_for_platforms(
        {"run_success": True, "screenshot_saved": True},
        [],
    ) is True
    assert manager._keyword_state_complete_for_platforms(
        {
            "platform_states": {
                "doubao": {"run_success": True, "screenshot_saved": True},
                "deepseek": {"run_success": True, "screenshot_saved": False},
            }
        },
        ["豆包", "DeepSeek"],
    ) is False
    assert manager._keyword_state_complete_for_platforms(
        {
            "platform_states": {
                "doubao": {"run_success": True, "screenshot_saved": True},
                "deepseek": {"run_success": True, "screenshot_saved": True},
            }
        },
        ["豆包", "DeepSeek"],
    ) is True
    assert manager._keyword_state_complete_for_platforms(
        {"platform": "通义千问", "run_success": True, "screenshot_saved": True},
        ["tongyi"],
    ) is True
    assert manager._keyword_state_complete_for_platforms(
        {"platform": "通义千问", "run_success": True, "screenshot_saved": True},
        ["tongyi", "doubao"],
    ) is False


def test_recognition_notification_idempotency_characterizes_stable_payload(manager):
    class _Notifier:
        webhook_url = " https://qy.example/webhook "

    identity = manager._build_notification_idempotency(
        _Notifier(),
        task={"task_id": "task-1", "name": "品牌A任务"},
        batch={"task_name": "批次任务名", "brands": ["品牌A"]},
        completed_keywords=["关键词1"],
        supplemented_keywords=["关键词2"],
        detected_platforms=["doubao", "deepseek"],
        image_count=-2,
    )

    run_date = local_today().isoformat()
    assert identity == {
        "webhook_url": "https://qy.example/webhook",
        "task_id": "task-1",
        "task_name": "批次任务名",
        "channel": "recognition_detected_images",
        "run_date": run_date,
        "round_id": f"recognition_detected_images:task-1:{run_date}",
        "payload_hash": build_payload_hash({
            "brands": ["品牌A"],
            "completed_keywords": ["关键词1"],
            "supplemented_keywords": ["关键词2"],
            "detected_platforms": ["doubao", "deepseek"],
            "image_count": 0,
        }),
    }


def test_matched_pairs_dedupe_expand_and_leave_remaining_platforms_stable(manager):
    serialized = manager._serialize_matched_pairs(
        [
            {"keyword": "品牌A 排行", "brand": "品牌A", "platforms": ["豆包", "DeepSeek", "豆包"]},
            {"keyword": "品牌A 排行", "brand": "品牌A", "platforms": ["doubao", "deepseek"]},
            {"keyword": "", "brand": "品牌A", "platforms": ["kimi"]},
        ]
    )

    assert serialized == [
        {"keyword": "品牌A 排行", "brand": "品牌A", "platforms": ["doubao", "deepseek"]}
    ]
    assert manager._expand_matched_pair_slots(serialized) == [
        {"keyword": "品牌A 排行", "brand": "品牌A", "platforms": ["doubao"]},
        {"keyword": "品牌A 排行", "brand": "品牌A", "platforms": ["deepseek"]},
    ]

    remaining = manager._remaining_current_platforms_after_match(
        {
            "keyword": "品牌A 排行",
            "brands": ["品牌A"],
            "platforms": ["doubao", "deepseek", "kimi"],
        },
        [{"keyword": "品牌A 排行", "brand": "品牌A", "platforms": ["豆包"]}],
    )

    assert remaining == ["deepseek", "kimi"]


def test_task_matched_pairs_use_platform_hint_for_multi_platform_current_item(manager):
    current_item = {
        "task_name": "品牌A任务",
        "keyword": "品牌A 评测",
        "brands": ["品牌A"],
        "platforms": ["doubao", "deepseek"],
    }

    hinted = manager._build_task_matched_pairs(
        task_name="品牌A任务",
        matched_brands=["品牌A"],
        current_item=current_item,
        guide_items=[current_item],
        platform_hint="DeepSeek",
    )
    ambiguous = manager._build_task_matched_pairs(
        task_name="品牌A任务",
        matched_brands=["品牌A"],
        current_item=current_item,
        guide_items=[current_item],
        platform_hint="",
    )

    assert hinted == [{"keyword": "品牌A 评测", "brand": "品牌A", "platforms": ["deepseek"]}]
    assert ambiguous == [{"keyword": "品牌A 评测", "brand": "品牌A", "platforms": []}]


def test_prepare_send_images_skips_missing_files_and_uses_text_platform_slot(manager, tmp_path):
    rendered = tmp_path / "rendered_deepseek.jpg"
    batch = {
        "task_name": "品牌A任务",
        "brands": ["品牌A"],
        "task": {"platform_candidates": ["doubao", "deepseek"]},
        "matched_pairs": [
            {"keyword": "品牌A 评测", "brand": "品牌A", "platforms": ["DeepSeek"]}
        ],
        "image_items": [
            {"path": str(tmp_path / "missing.jpg"), "ocr_text": "", "source_text": ""},
            {"path": "", "ocr_text": "", "source_text": "DeepSeek 回答提到品牌A"},
        ],
    }

    with patch.object(manager, "_build_text_mode_send_image", return_value=str(rendered)) as render:
        processed_paths, detected_platforms = manager._prepare_send_images(batch, {})

    assert processed_paths == [str(rendered)]
    assert detected_platforms == ["deepseek"]
    assert render.call_args.kwargs["platform_name"] == "deepseek"
    assert render.call_args.kwargs["keyword"] == "品牌A 评测"


def test_keyword_updates_keep_platform_slots_when_later_screenshots_are_missing(manager, tmp_path):
    first_image = tmp_path / "doubao.jpg"
    first_image.write_bytes(b"fake-image")

    updates = manager._build_keyword_updates_from_batch(
        {
            "matched_pairs": [
                {
                    "keyword": "品牌A 评测",
                    "brand": "品牌A",
                    "platforms": ["doubao", "deepseek"],
                }
            ]
        },
        image_paths=[str(first_image)],
        detected_platforms=[],
    )

    assert updates == [
        {
            "keyword": "品牌A 评测",
            "brand": "品牌A",
            "run_success": True,
            "screenshot_saved": True,
            "failure_reason": "",
            "platform": "doubao",
            "image_path": str(first_image),
        },
        {
            "keyword": "品牌A 评测",
            "brand": "品牌A",
            "run_success": True,
            "screenshot_saved": False,
            "failure_reason": "screenshot_save_failed",
            "platform": "deepseek",
            "image_path": "",
        },
    ]


def test_runtime_completed_slots_prefer_item_level_matched_pairs_over_global_order(manager):
    completed = {}
    manager._add_runtime_completed_slots_from_batch(
        completed,
        {
            "task_name": "品牌A任务",
            "matched_pairs": [
                {"keyword": "词1", "brand": "品牌A", "platforms": ["doubao"]},
                {"keyword": "词2", "brand": "品牌A", "platforms": ["deepseek"]},
            ],
            "image_items": [
                {
                    "path": "",
                    "source_text": "DeepSeek 回答",
                    "matched_pairs": [
                        {"keyword": "词2", "brand": "品牌A", "platforms": ["deepseek"]},
                    ],
                }
            ],
        },
    )

    assert completed == {
        ("品牌A任务", "词2", "品牌a"): {"deepseek"},
    }
