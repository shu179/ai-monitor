from pathlib import Path

from core.cloud_agent_status_store import CloudAgentStatusStore


def _agent_status_entity(**overrides):
    entity = {
        "type": "agent_command_status",
        "id": "command-001",
        "workspace_id": 3,
        "target_device_id": "mac-1",
        "target_role": "desktop",
        "status": "completed",
        "idempotency_key": "agent-key-1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "result_chunks": [
            {
                "command_id": "command-001",
                "seq": 0,
                "payload_json": {"text": "done"},
                "is_final": True,
            }
        ],
    }
    entity.update(overrides)
    return entity


def test_cloud_agent_status_store_records_diagnostics(tmp_path: Path):
    store = CloudAgentStatusStore(tmp_path / "agent_status.sqlite3")

    result = store.apply_status("https://api.example.com|3|2", _agent_status_entity())
    diagnostics = store.diagnostics(
        {
            "base_url": "https://api.example.com",
            "user": {"workspace_id": 3, "id": 2},
        }
    )

    assert result["ok"] is True
    assert result["command_id"] == "command-001"
    assert diagnostics["total"] == 1
    assert diagnostics["by_status"] == {"completed": 1}
    assert diagnostics["newest"][0]["id"] == "command-001"
    assert diagnostics["newest"][0]["chunk_count"] == 1


def test_cloud_agent_status_store_merges_result_chunks(tmp_path: Path):
    store = CloudAgentStatusStore(tmp_path / "agent_status.sqlite3")
    identity_key = "https://api.example.com|3|2"

    assert store.apply_status(identity_key, _agent_status_entity())["ok"] is True
    result = store.apply_status(
        identity_key,
        _agent_status_entity(
            status="completed",
            result_chunks=[
                {
                    "command_id": "command-001",
                    "seq": 1,
                    "payload_json": {"text": "postscript"},
                    "is_final": True,
                }
            ],
        ),
    )

    assert result["ok"] is True
    assert result["result_chunks"] == 2
    diagnostics = store.diagnostics({"base_url": "https://api.example.com", "user": {"workspace_id": 3, "id": 2}})
    assert diagnostics["newest"][0]["chunk_count"] == 2


def test_cloud_agent_status_store_rejects_invalid_shape(tmp_path: Path):
    store = CloudAgentStatusStore(tmp_path / "agent_status.sqlite3")

    result = store.apply_status("https://api.example.com|3|2", {"type": "agent_command", "id": "cmd"})

    assert result["ok"] is False
    assert result["updated"] == 0
