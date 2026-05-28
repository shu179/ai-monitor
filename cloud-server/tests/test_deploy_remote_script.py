from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import deploy_remote


def test_build_rsync_command_excludes_runtime_and_mac_junk() -> None:
    command = deploy_remote.build_rsync_command(
        ssh_host="tencent",
        remote_dir="/opt/surfaced/cloud-server",
    )

    assert command[:3] == ["rsync", "-az", "--delete"]
    assert "--exclude" in command
    assert ".env" in command
    assert ".venv" in command
    assert "._*" in command
    assert command[-2] == f"{deploy_remote.ROOT}/"
    assert command[-1] == "tencent:/opt/surfaced/cloud-server/"


def test_render_remote_script_includes_backup_cleanup_and_rebuild_steps() -> None:
    script = deploy_remote.render_remote_script(
        remote_dir="/opt/surfaced/cloud-server",
        backup_dir="/opt/surfaced/backups",
        health_url="http://127.0.0.1:8080",
        db_backup=True,
        code_backup=True,
        rebuild=True,
    )

    assert 'find "$REMOTE_DIR" \\( -name "._*" -o -name ".DS_Store" \\) -delete' in script
    assert 'step=db_backup' in script
    assert 'docker compose exec -T postgres' in script
    assert 'step=code_backup' in script
    assert 'cloud-server-code-${TS}.tar.gz' in script
    assert 'step=rebuild' in script
    assert 'docker compose up -d --build' in script
    assert 'python3 -m scripts.deploy_check --base-url "$HEALTH_URL"' in script


def test_render_remote_script_can_skip_optional_steps() -> None:
    script = deploy_remote.render_remote_script(
        remote_dir="/opt/surfaced/cloud-server",
        backup_dir="/opt/surfaced/backups",
        health_url="http://127.0.0.1:8080",
        db_backup=False,
        code_backup=False,
        rebuild=False,
    )

    assert 'step=db_backup' not in script
    assert 'step=code_backup' not in script
    assert 'docker compose up -d --build' not in script
    assert 'step=deploy_check' in script


def test_main_dry_run_prints_commands_without_running() -> None:
    with patch("scripts.deploy_remote.run") as run_mock, patch("builtins.print") as print_mock:
        result = deploy_remote.main(
            [
                "--ssh-host",
                "tencent",
                "--remote-dir",
                "/opt/surfaced/cloud-server",
                "--dry-run",
            ]
        )

    assert result == 0
    run_mock.assert_not_called()
    printed = "\n".join(" ".join(str(part) for part in call.args) for call in print_mock.call_args_list)
    assert "rsync:" in printed
    assert "ssh:" in printed
    assert "REMOTE_DIR=" in printed
