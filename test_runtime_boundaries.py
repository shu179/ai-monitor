from pathlib import Path


def test_runtime_code_does_not_import_main_entrypoint():
    repo_root = Path(__file__).resolve().parent
    offenders: list[str] = []
    skipped_dirs = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "docs",
        "node_modules",
        "site-packages",
        "venv",
    }

    for path in repo_root.rglob("*.py"):
        relative = path.relative_to(repo_root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if any(part in skipped_dirs for part in relative.parts):
            continue
        if path.name.startswith("test_") or "tests" in relative.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "from main import" in text or "import main" in text:
            offenders.append(str(relative))

    assert offenders == []
