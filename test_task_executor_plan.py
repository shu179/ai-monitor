from pathlib import Path

from core.task_executor_plan import build_task_execution_plan, historical_query_result


def test_build_task_execution_plan_applies_runtime_mode_to_all_keywords(tmp_path):
    keywords = [
        {
            "keyword": "品牌A 评测",
            "brand": "品牌A",
            "platforms": ["doubao", "deepseek"],
            "mode": "browser",
        },
        {
            "keyword": "品牌A 识别",
            "brand": "品牌A",
            "platforms": ["kimi"],
            "mode": "recognition",
        },
    ]

    plan = build_task_execution_plan(
        task={"fixed_screenshot_enabled": True, "fixed_screenshot_count": 1},
        keywords=keywords,
        default_brand="品牌A",
        runtime_mode="api",
        historical_success_map={},
        manual_test_replay_completed_keywords=False,
    )

    assert [entry["keyword"] for entry in plan.executable_entries] == ["品牌A 评测", "品牌A 识别"]
    assert [entry["mode"] for entry in plan.executable_entries] == ["api", "api"]
    assert plan.ordered_platforms == ["doubao", "deepseek", "kimi"]
    assert plan.total_queries == 3
    assert plan.expected_query_count == 3
    assert plan.fixed_screenshot_target == 3


def test_build_task_execution_plan_excludes_recognition_without_runtime_mode():
    keywords = [
        {
            "keyword": "品牌A 评测",
            "brand": "品牌A",
            "platforms": ["doubao"],
            "mode": "browser",
        },
        {
            "keyword": "品牌A 识别",
            "brand": "品牌A",
            "platforms": ["kimi"],
            "mode": "recognition",
        },
    ]

    plan = build_task_execution_plan(
        task={},
        keywords=keywords,
        default_brand="品牌A",
        runtime_mode="",
        historical_success_map={},
        manual_test_replay_completed_keywords=False,
    )

    assert [entry["keyword"] for entry in plan.executable_entries] == ["品牌A 评测"]
    assert plan.ordered_platforms == ["doubao"]
    assert plan.total_queries == 1


def test_historical_query_result_requires_existing_screenshot_for_fixed_screenshot(tmp_path):
    entry = {
        "keyword": "品牌A 评测",
        "brand": "品牌A",
        "platforms": ["doubao"],
        "mode": "browser",
        "kw_entry": {},
    }
    screenshot_path = tmp_path / "hit.jpg"
    historical_success = {
        ("品牌A 评测", "doubao", "品牌A"): {
            "keyword": "品牌A 评测",
            "platform": "doubao",
            "brand": "品牌A",
            "rank": 1,
            "screenshot": str(screenshot_path),
        }
    }

    assert historical_query_result(
        entry,
        "doubao",
        historical_success_map=historical_success,
        fixed_screenshot_enabled=True,
        manual_test_replay_completed_keywords=False,
    ) is None

    Path(screenshot_path).write_bytes(b"image")
    assert historical_query_result(
        entry,
        "doubao",
        historical_success_map=historical_success,
        fixed_screenshot_enabled=True,
        manual_test_replay_completed_keywords=False,
    ) == historical_success[("品牌A 评测", "doubao", "品牌A")]


def test_manual_test_plan_replays_historical_successes(tmp_path):
    entry = {
        "keyword": "品牌A 评测",
        "brand": "品牌A",
        "platforms": ["doubao"],
        "mode": "browser",
        "kw_entry": {},
    }
    historical_success = {
        ("品牌A 评测", "doubao", "品牌A"): {
            "keyword": "品牌A 评测",
            "platform": "doubao",
            "brand": "品牌A",
            "rank": 1,
            "screenshot": "",
        }
    }

    assert historical_query_result(
        entry,
        "doubao",
        historical_success_map=historical_success,
        fixed_screenshot_enabled=False,
        manual_test_replay_completed_keywords=True,
    ) is None
