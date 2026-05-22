from __future__ import annotations

import yaml

from core.config_watcher import load_config, save_config


def test_load_config_restores_cloud_task_webhooks_from_secret_identities_and_common_fallback(tmp_path):
    config_path = tmp_path / "config.yaml"
    local_path = tmp_path / "config.local.yaml"

    config_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "task_id": "cloud_2",
                        "name": "即搜AI",
                        "brand": "即搜AI",
                        "cloud_task_id": 2,
                        "cloud_task_key": "jisou-ai-wuhan-geo",
                        "webhook_url": "",
                    },
                    {
                        "task_id": "cloud_23",
                        "name": "盛世兴华",
                        "brand": "盛世兴华",
                        "cloud_task_id": 23,
                        "cloud_task_key": "local-legacy_c90d5322dd02",
                        "webhook_url": "",
                    },
                    {
                        "task_id": "legacy_local_blank",
                        "name": "本地空任务",
                        "brand": "本地空任务",
                        "webhook_url": "",
                    },
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    local_path.write_text(
        yaml.safe_dump(
            {
                "task_secrets": {
                    "legacy_2fc71313da15": {
                        "task_id": "legacy_2fc71313da15",
                        "task_name": "即搜",
                        "cloud_task_id": "2",
                        "cloud_task_key": "jisou-ai-wuhan-geo",
                        "webhook_url": "https://example.com/common-webhook",
                    },
                    "cloud_21": {
                        "task_id": "cloud_21",
                        "task_name": "上腾科技",
                        "cloud_task_id": "21",
                        "cloud_task_key": "local-legacy_38d053a94f8a",
                        "webhook_url": "https://example.com/common-webhook",
                    },
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    loaded = load_config(str(config_path))
    tasks = {str(task.get("task_id") or ""): task for task in loaded.get("tasks", []) if isinstance(task, dict)}

    assert tasks["cloud_2"]["webhook_url"] == "https://example.com/common-webhook"
    assert tasks["cloud_23"]["webhook_url"] == "https://example.com/common-webhook"
    assert tasks["legacy_local_blank"]["webhook_url"] == ""


def test_save_config_persists_cloud_task_secret_metadata(tmp_path):
    config_path = tmp_path / "config.yaml"

    save_config(
        {
            "tasks": [
                {
                    "task_id": "cloud_2",
                    "name": "即搜AI",
                    "brand": "即搜AI",
                    "cloud_task_id": 2,
                    "cloud_task_key": "jisou-ai-wuhan-geo",
                    "webhook_url": "https://example.com/common-webhook",
                }
            ]
        },
        str(config_path),
    )

    public_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    local_config = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8")) or {}
    entry = local_config["task_secrets"]["cloud_2"]

    assert public_config["tasks"][0]["webhook_url"] == ""
    assert entry["task_id"] == "cloud_2"
    assert entry["task_name"] == "即搜AI"
    assert entry["cloud_task_id"] == "2"
    assert entry["cloud_task_key"] == "jisou-ai-wuhan-geo"
    assert entry["webhook_url"] == "https://example.com/common-webhook"
