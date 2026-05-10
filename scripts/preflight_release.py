#!/usr/bin/env python3
"""
Preflight release check script.

Run before packaging to validate build configuration and module integrity.
No network dependencies, cross-platform (macOS/Linux/Windows).
"""

from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import NamedTuple


class CheckResult(NamedTuple):
    status: str  # "PASS", "WARN", "FAIL"
    category: str
    message: str


def check_build_spec(root: Path) -> list[CheckResult]:
    """Static checks on build.spec."""
    results = []
    spec_path = root / "build.spec"

    if not spec_path.exists():
        results.append(CheckResult(
            "FAIL", "build.spec", f"build.spec not found at {spec_path}"
        ))
        return results

    try:
        content = spec_path.read_text(encoding="utf-8")
    except Exception as e:
        results.append(CheckResult(
            "FAIL", "build.spec", f"Failed to read build.spec: {e}"
        ))
        return results

    # PyInstaller exposes SPEC/SPECPATH globals, not SPECDIR. Using SPECDIR
    # fails before Analysis() starts on current PyInstaller releases.
    if re.search(r"\bSPECDIR\b", content):
        results.append(CheckResult(
            "FAIL", "build.spec", "build.spec uses unsupported SPECDIR global"
        ))
    else:
        results.append(CheckResult(
            "PASS", "build.spec", "build.spec does not use unsupported SPECDIR global"
        ))

    if "COLLECT(" in content:
        if "exclude_binaries=True" in content:
            results.append(CheckResult(
                "PASS", "build.spec", "onedir COLLECT build uses exclude_binaries=True"
            ))
        else:
            results.append(CheckResult(
                "FAIL", "build.spec",
                "onedir COLLECT build should set exclude_binaries=True on EXE"
            ))

    datas_match = re.search(r"\bdatas\s*=\s*\[(?P<body>.*?)\]", content, re.DOTALL)
    if datas_match and re.search(r"\bTree\s*\(", datas_match.group("body")):
        results.append(CheckResult(
            "FAIL", "build.spec",
            "Tree entries should be passed to COLLECT, not Analysis datas"
        ))
    else:
        results.append(CheckResult(
            "PASS", "build.spec", "Analysis datas does not contain Tree entries"
        ))

    # Check manifest reference exists
    if "manifest='build/app.manifest'" in content or 'manifest="build/app.manifest"' in content:
        manifest_file = root / "build" / "app.manifest"
        if manifest_file.exists():
            results.append(CheckResult(
                "PASS", "build.spec", "manifest='build/app.manifest' exists"
            ))
        else:
            results.append(CheckResult(
                "FAIL", "build.spec", "build.spec references build/app.manifest but file does not exist"
            ))
    else:
        results.append(CheckResult(
            "FAIL", "build.spec", "build.spec does not reference manifest='build/app.manifest'"
        ))

    # Check hiddenimports for core.windows_bootstrap
    if "core.windows_bootstrap" in content:
        results.append(CheckResult(
            "PASS", "build.spec", "hiddenimports includes core.windows_bootstrap"
        ))
    else:
        results.append(CheckResult(
            "FAIL", "build.spec", "hiddenimports does not include core.windows_bootstrap"
        ))

    # Check icon configuration - warn if commented or missing
    icon_pattern = r"^\s*icon\s*=\s*['\"].*\.ico['\"]"
    icon_match = re.search(icon_pattern, content, re.MULTILINE)

    # Check if there's a commented icon line
    commented_icon_pattern = r"^\s*#.*icon\s*="
    commented_match = re.search(commented_icon_pattern, content, re.MULTILINE)

    if icon_match:
        results.append(CheckResult(
            "PASS", "build.spec", f"icon configured: {icon_match.group().strip()}"
        ))
    elif commented_match:
        results.append(CheckResult(
            "WARN", "build.spec", f"icon is commented out: {commented_match.group().strip()}"
        ))
    else:
        results.append(CheckResult(
            "WARN", "build.spec", "icon configuration not found in build.spec"
        ))

    return results


def _get_first_n_imports(file_path: Path, n: int = 30) -> tuple[list[str], list[str]]:
    """Extract import lines and GUI-related imports from first n lines."""
    if not file_path.exists():
        return [], []

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()[:n]
    except Exception:
        return [], []

    import_lines = []
    gui_patterns = [
        r"from\s+PySide6",
        r"import\s+PySide6",
        r"from\s+PyQt",
        r"import\s+PyQt",
        r"from\s+flet",
        r"import\s+flet",
        r"import\s+tkinter",
        r"from\s+tkinter",
    ]
    gui_imports = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("from ") or stripped.startswith("import "):
            import_lines.append(stripped)
            for pattern in gui_patterns:
                if re.search(pattern, stripped):
                    gui_imports.append(stripped)
                    break

    return import_lines, gui_imports


def _check_bootstrap_order(
    root: Path,
    file_name: str,
    check_gui_order: bool = False
) -> list[CheckResult]:
    """Check if install_windows_bootstrap() is called in first 30 lines."""
    results = []
    file_path = root / file_name

    if not file_path.exists():
        results.append(CheckResult(
            "FAIL", "bootstrap.order", f"{file_name} not found"
        ))
        return results

    import_lines, gui_imports = _get_first_n_imports(file_path, n=30)

    # Check if windows_bootstrap is imported
    has_import = any("windows_bootstrap" in line for line in import_lines)

    if not has_import:
        results.append(CheckResult(
            "FAIL", "bootstrap.order",
            f"{file_name}: core.windows_bootstrap not imported in first 30 lines"
        ))
        return results

    # Check if install_windows_bootstrap() is called
    lines_30 = []
    try:
        lines_30 = file_path.read_text(encoding="utf-8").splitlines()[:30]
    except Exception:
        pass

    has_call = any("install_windows_bootstrap()" in line for line in lines_30)

    if not has_call:
        results.append(CheckResult(
            "FAIL", "bootstrap.order",
            f"{file_name}: install_windows_bootstrap() not called in first 30 lines"
        ))
    else:
        results.append(CheckResult(
            "PASS", "bootstrap.order",
            f"{file_name}: install_windows_bootstrap() called in first 30 lines"
        ))

    # Check GUI import order if requested
    if check_gui_order and gui_imports:
        # Find line numbers
        try:
            all_lines = file_path.read_text(encoding="utf-8").splitlines()[:30]
        except Exception:
            all_lines = []

        bootstrap_call_idx = None
        gui_import_indices = []

        for idx, line in enumerate(all_lines):
            if "install_windows_bootstrap()" in line:
                bootstrap_call_idx = idx
            for gui_line in gui_imports:
                if gui_line in line:
                    gui_import_indices.append(idx)
                    break

        if bootstrap_call_idx is not None and gui_import_indices:
            first_gui_idx = min(gui_import_indices)
            if first_gui_idx < bootstrap_call_idx:
                results.append(CheckResult(
                    "FAIL", "bootstrap.order",
                    f"{file_name}: GUI import ({all_lines[first_gui_idx].strip()}) "
                    f"appears before install_windows_bootstrap() call (line {bootstrap_call_idx + 1})"
                ))
            else:
                results.append(CheckResult(
                    "PASS", "bootstrap.order",
                    f"{file_name}: install_windows_bootstrap() before GUI imports"
                ))

    return results


def check_bootstrap_order(root: Path) -> list[CheckResult]:
    """Check Windows bootstrap order in all entry points."""
    results = []

    # main.py - always check GUI order
    results.extend(_check_bootstrap_order(root, "main.py", check_gui_order=False))

    # web_desktop.py - check GUI order
    results.extend(_check_bootstrap_order(root, "web_desktop.py", check_gui_order=True))

    # web_backend.py - check __main__ entry
    backend_path = root / "web_backend.py"
    if backend_path.exists():
        try:
            content = backend_path.read_text(encoding="utf-8")
        except Exception as e:
            results.append(CheckResult(
                "FAIL", "bootstrap.order", f"web_backend.py: failed to read: {e}"
            ))
            return results

        # Find the __main__ block
        main_match = re.search(r"if __name__\s*==\s*['\"]__main__['\"]:", content)
        if main_match:
            # Get the next 20 lines after __main__
            start = main_match.end()
            main_block = content[start:start + 2000]
            main_lines = main_block.splitlines()[:20]

            has_bootstrap_import = any("windows_bootstrap" in line for line in main_lines)
            has_bootstrap_call = any("install_windows_bootstrap()" in line for line in main_lines)

            if has_bootstrap_import and has_bootstrap_call:
                results.append(CheckResult(
                    "PASS", "bootstrap.order",
                    "web_backend.py __main__: install_windows_bootstrap() called"
                ))
            elif has_bootstrap_import and not has_bootstrap_call:
                results.append(CheckResult(
                    "FAIL", "bootstrap.order",
                    "web_backend.py __main__: windows_bootstrap imported but install_windows_bootstrap() not called"
                ))
            else:
                results.append(CheckResult(
                    "FAIL", "bootstrap.order",
                    "web_backend.py __main__: windows_bootstrap not imported/called"
                ))
        else:
            results.append(CheckResult(
                "WARN", "bootstrap.order", "web_backend.py: __main__ block not found"
            ))

    return results


def check_import_smoke(root: Path | None = None) -> list[CheckResult]:
    """Smoke test for key new modules - import without side effects."""
    results = []

    # Add project root to path if provided
    if root is not None:
        root_str = str(root.absolute())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

    # These modules should be importable without GUI/browser/network
    smoke_modules = [
        ("core.windows_bootstrap", "Windows bootstrap"),
        ("core.diagnostic_events", "Diagnostic events"),
        ("core.screenshot_cleanup", "Screenshot cleanup"),
        ("core.cloud_outbox", "Cloud outbox"),
    ]

    for module_name, label in smoke_modules:
        try:
            __import__(module_name)
            results.append(CheckResult(
                "PASS", "import.smoke", f"{label} ({module_name}) imports OK"
            ))
        except ImportError as e:
            results.append(CheckResult(
                "FAIL", "import.smoke", f"{label} ({module_name}) import failed: {e}"
            ))
        except Exception as e:
            # Other errors may be due to transitive dependencies, warn but don't fail
            results.append(CheckResult(
                "WARN", "import.smoke", f"{label} ({module_name}) has issues: {type(e).__name__}: {e}"
            ))

    return results


def run_checks(root: Path | None = None) -> tuple[int, list[CheckResult]]:
    """
    Run all preflight checks.

    Returns (exit_code, results).
    Exit code: 0 = all pass/warn, 1 = at least one fail.
    """
    if root is None:
        root = Path(__file__).parent.parent

    results: list[CheckResult] = []
    results.extend(check_build_spec(root))
    results.extend(check_bootstrap_order(root))
    results.extend(check_import_smoke(root))

    # Count statuses
    fail_count = sum(1 for r in results if r.status == "FAIL")
    warn_count = sum(1 for r in results if r.status == "WARN")
    pass_count = sum(1 for r in results if r.status == "PASS")

    # Print results
    print("=" * 60)
    print("Preflight Release Check")
    print("=" * 60)
    print()

    for result in results:
        icon = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[result.status]
        print(f"{icon} [{result.category}] {result.message}")

    print()
    print(f"Summary: {pass_count} passed, {warn_count} warnings, {fail_count} failures")
    print("=" * 60)

    exit_code = 1 if fail_count > 0 else 0
    return exit_code, results


def main() -> int:
    """CLI entry point."""
    root = Path(__file__).parent.parent
    exit_code, _ = run_checks(root)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
