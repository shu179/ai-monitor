from pathlib import Path
from unittest.mock import patch

import pytest

import core.daily_task_state as dts
from core import task_executor_impl
from core.task_notifications import send_task_notifications


class _ReplayBrowserPlatform:
    def __init__(self, platform_name: str, screenshot_dir: Path, calls: list[str]) -> None:
        self.platform_name = platform_name
        self._screenshot_dir = screenshot_dir
        self._calls = calls
        self.last_error = ""
        self.last_answer_text = ""
        self.last_screenshot_meta = {"highlight_count": 1}
        self.last_references = []
        self.last_body_references = []
        self.last_run_recovered_manually = False
        self.page = object()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def start(self):
        return self

    def close(self) -> None:
        return None

    def search(self, keyword: str, brand: str) -> tuple[int, str]:
        self._calls.append(f"browser:{self.platform_name}:{keyword}")
        if self.platform_name == "deepseek":
            self.last_error = "浏览器页面崩溃"
            raise RuntimeError(self.last_error)

        screenshot_path = self._screenshot_dir / f"{self.platform_name}_{keyword}.jpg"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(b"fake-browser-image")
        self.last_answer_text = f"{brand} appears on {self.platform_name}"
        return 1, str(screenshot_path)


@pytest.fixture()
def isolated_task_state(tmp_path, monkeypatch):
    original_state_path = dts.STATE_PATH
    monkeypatch.setenv("AIBRANDMONITOR_DATA_DIR", str(tmp_path))
    dts.STATE_PATH = tmp_path / "daily_task_status.json"
    try:
        yield tmp_path
    finally:
        dts.STATE_PATH = original_state_path


def test_mixed_six_platform_replay_keeps_order_and_reports_failures(isolated_task_state):
    screenshot_dir = isolated_task_state / "screenshots"
    calls: list[str] = []
    diagnostics: list[dict] = []
    issues: list[dict] = []

    task = {
        "name": "六平台 characterization",
        "task_id": "platform_characterization_six_modes",
        "keywords": [
            {
                "keyword": "品牌A 浏览器问答",
                "brand": "品牌A",
                "platforms": ["doubao", "deepseek"],
                "mode": "browser",
            },
            {
                "keyword": "品牌B API问答",
                "brand": "品牌B",
                "platforms": ["kimi", "tongyi"],
                "mode": "api",
            },
            {
                "keyword": "品牌C 智能问答",
                "brand": "品牌C",
                "platforms": ["yuanbao"],
                "mode": "smart",
            },
            {
                "keyword": "品牌D 浏览器问答",
                "brand": "品牌D",
                "platforms": ["wenxin"],
                "mode": "browser",
            },
        ],
    }

    def create_platform(platform_name: str, *, config=None, inspect=False, stop_checker=None):
        del config, inspect, stop_checker
        return _ReplayBrowserPlatform(platform_name, screenshot_dir, calls)

    def run_api_task(platform_name: str, keyword: str, brand: str, config: dict, **kwargs):
        del config, kwargs
        calls.append(f"api:{platform_name}:{keyword}")
        if platform_name == "tongyi":
            return {
                "rank": 99,
                "screenshot": None,
                "answer_text": "",
                "evidence": "",
                "error_message": "未配置 api_key",
                "highlight_count": 0,
                "mode": "api",
            }
        screenshot_path = screenshot_dir / f"{platform_name}_{keyword}.jpg"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(b"fake-api-image")
        return {
            "rank": 1,
            "screenshot": str(screenshot_path),
            "answer_text": f"{brand} appears through API",
            "evidence": brand,
            "error_message": "",
            "highlight_count": 0,
            "mode": "api",
        }

    def run_smart_browser_task(**kwargs):
        platform_name = kwargs["platform_name"]
        keyword = kwargs["keyword"]
        brand = kwargs["brand"]
        calls.append(f"smart:{platform_name}:{keyword}")
        screenshot_path = screenshot_dir / f"{platform_name}_{keyword}.jpg"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(b"fake-smart-image")
        return {
            "rank": 1,
            "screenshot": str(screenshot_path),
            "answer_text": f"{brand} appears through smart browser",
            "evidence": brand,
            "error_message": "",
            "highlight_count": 2,
            "mode": "smart",
        }

    def record_diagnostic(task_name, keyword, platform_name, brand, message, **kwargs):
        diagnostics.append(
            {
                "task_name": task_name,
                "keyword": keyword,
                "platform": platform_name,
                "brand": brand,
                "message": message,
                "details": kwargs.get("details") or {},
            }
        )
        return f"diag-{len(diagnostics)}"

    with (
        patch("core.task_executor_browser.create_browser_platform", side_effect=create_platform),
        patch("core.task_executor_impl._run_api_task", side_effect=run_api_task),
        patch("core.task_executor_impl._run_smart_browser_task", side_effect=run_smart_browser_task),
        patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}),
        patch("core.task_executor_impl._record_result_history", return_value=None),
        patch("core.task_executor_impl._record_diagnostic", side_effect=record_diagnostic),
        patch(
            "core.task_executor_impl._send_task_notifications",
            return_value={"attempted": True, "success": True, "found_results": 4, "error_message": ""},
        ),
    ):
        results, report = task_executor_impl.run_task_group(
            task,
            {},
            {},
            return_report=True,
            issue_callback=issues.append,
        )

    assert calls == [
        "browser:doubao:品牌A 浏览器问答",
        "browser:deepseek:品牌A 浏览器问答",
        "api:kimi:品牌B API问答",
        "api:tongyi:品牌B API问答",
        "smart:yuanbao:品牌C 智能问答",
        "browser:wenxin:品牌D 浏览器问答",
    ]
    assert [item["platform"] for item in results] == [
        "doubao",
        "deepseek",
        "kimi",
        "tongyi",
        "yuanbao",
        "wenxin",
    ]
    assert [item["mode"] for item in results] == [
        "browser",
        "browser",
        "api",
        "api",
        "smart",
        "browser",
    ]
    assert {item["platform"]: item["rank"] for item in results} == {
        "doubao": 1,
        "deepseek": 99,
        "kimi": 1,
        "tongyi": 99,
        "yuanbao": 1,
        "wenxin": 1,
    }
    assert report["mode"] == "mixed"
    assert report["attempted_queries"] == 6
    assert report["success_queries"] == 4
    assert report["failed_queries"] == 2
    assert report["round_status"] == "partial"
    assert report["query_round_status"] == "partial"
    assert report["task_status"] == "partial"
    assert report["failure_kind"] == "structural"
    assert report["task_failure_kind"] == "structural"

    failures = {item["platform"]: item for item in report["failed_query_details"]}
    assert failures["deepseek"]["failure_type"] == "temporary_error"
    assert failures["tongyi"]["failure_type"] == "structural_error"
    assert [item["platform"] for item in diagnostics] == ["deepseek", "tongyi"]
    assert [item["platform"] for item in issues] == ["deepseek", "tongyi"]


def test_missing_screenshot_keeps_notification_fallback_explainable(tmp_path):
    screenshot = tmp_path / "doubao.jpg"
    screenshot.write_bytes(b"fake-image")
    keywords = [
        {"keyword": "品牌A 豆包", "brand": "品牌A", "platforms": ["doubao"], "mode": "browser"},
        {"keyword": "品牌A Kimi", "brand": "品牌A", "platforms": ["kimi"], "mode": "api"},
    ]
    all_results = [
        {
            "keyword": "品牌A 豆包",
            "brand": "品牌A",
            "platform": "doubao",
            "rank": 1,
            "screenshot": str(screenshot),
            "mode": "browser",
        },
        {
            "keyword": "品牌A Kimi",
            "brand": "品牌A",
            "platform": "kimi",
            "rank": 1,
            "screenshot": "",
            "mode": "api",
        },
    ]

    result = send_task_notifications(
        {"name": "缺截图 characterization", "task_id": "missing_screenshot_characterization"},
        "品牌A",
        notifier=None,
        keywords=keywords,
        all_results=all_results,
        expected_query_count=2,
    )

    assert result["attempted"] is False
    assert result["success"] is False
    assert result["found_results"] == 2
    assert "任务组未补齐" in result["error_message"]
