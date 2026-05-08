import ast
import main
from pathlib import Path

from core import task_executor
from core import task_executor_impl


def test_run_task_group_has_stable_core_entrypoint():
    assert task_executor.run_task_group is task_executor_impl.run_task_group
    assert main.run_task_group is task_executor.run_task_group


def test_run_task_group_handles_empty_keywords_with_report():
    results, report = task_executor.run_task_group(
        {"name": "空任务", "task_id": "empty_task", "keywords": []},
        {},
        return_report=True,
    )

    assert results == []
    assert report["task_id"] == "empty_task"
    assert report["round_status"] == "skipped"
    assert report["attempted_queries"] == 0


def test_web_backend_does_not_import_main_entrypoint():
    """The web backend must not reload main.py as a second module namespace."""

    source = Path("web_backend.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            forbidden_imports.extend(alias.name for alias in node.names if alias.name == "main")
        elif isinstance(node, ast.ImportFrom) and node.module == "main":
            forbidden_imports.append("from main import ...")

    assert forbidden_imports == []
