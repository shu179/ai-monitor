from unittest.mock import patch

import pytest

from core.recognition import ClipboardRecognitionManager


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
