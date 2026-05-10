"""Tests for preflight_release.py script."""

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts dir to path for import
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from preflight_release import (
    check_build_spec,
    check_bootstrap_order,
    check_import_smoke,
    run_checks,
    CheckResult,
)


@pytest.fixture
def tmp_project(tmp_path):
    """Create a temporary project structure."""
    # Create core dir with stub modules
    core_dir = tmp_path / "core"
    core_dir.mkdir()

    for module in ["windows_bootstrap", "diagnostic_events", "screenshot_cleanup", "cloud_outbox"]:
        (core_dir / f"{module}.py").write_text("# stub\n")

    # Create build dir with manifest
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "app.manifest").write_text("<?xml version='1.0'?>\n<assembly/>")

    # Create build.spec with correct config
    (tmp_path / "build.spec").write_text(
        "manifest='build/app.manifest'\n"
        "hiddenimports=['core.windows_bootstrap']\n"
    )

    # Create main.py with bootstrap
    (tmp_path / "main.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "from core.windows_bootstrap import install_windows_bootstrap\n"
        "install_windows_bootstrap()\n"
        "from core import load_config\n"
    )

    # Create web_desktop.py with correct bootstrap order
    (tmp_path / "web_desktop.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "from core.windows_bootstrap import install_windows_bootstrap\n"
        "install_windows_bootstrap()\n"
        "from PySide6.QtCore import QTimer\n"
    )

    # Create web_backend.py with __main__ bootstrap
    (tmp_path / "web_backend.py").write_text(
        "from core.windows_bootstrap import install_windows_bootstrap\n"
        "install_windows_bootstrap()\n"
        "\n"
        "def create_server():\n"
        "    pass\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    from core.windows_bootstrap import install_windows_bootstrap\n"
        "    install_windows_bootstrap()\n"
        "    server = create_server()\n"
        "    server.start()\n"
    )

    return tmp_path


@pytest.fixture
def tmp_project_missing_manifest(tmp_project):
    """Project with missing manifest file."""
    (tmp_project / "build" / "app.manifest").unlink()
    return tmp_project


@pytest.fixture
def tmp_project_commented_icon(tmp_project):
    """Project with commented icon."""
    # Overwrite build.spec with commented icon
    (tmp_project / "build.spec").write_text(
        """
a = Analysis(['main.py'], hiddenimports=['core.windows_bootstrap'])
exe = EXE(pyz, [], name='App', manifest='build/app.manifest',
          # icon='app.ico',
)
"""
    )
    return tmp_project


@pytest.fixture
def tmp_project_gui_before_bootstrap(tmp_path):
    """Project where GUI import comes before bootstrap."""
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    for module in ["windows_bootstrap", "diagnostic_events", "screenshot_cleanup", "cloud_outbox"]:
        (core_dir / f"{module}.py").write_text("# stub\n")

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "app.manifest").write_text("<?xml version='1.0'?>\n<assembly/>")

    # Create build.spec
    (tmp_path / "build.spec").write_text(
        "manifest='build/app.manifest'\n"
        "hiddenimports=['core.windows_bootstrap']\n"
    )

    # web_desktop.py with GUI BEFORE bootstrap - WRONG ORDER
    (tmp_path / "web_desktop.py").write_text(
        "import sys\n"
        "from PySide6.QtCore import QTimer\n"
        "from core.windows_bootstrap import install_windows_bootstrap\n"
        "install_windows_bootstrap()\n"
    )

    return tmp_path


class TestCheckBuildSpec:
    """Tests for check_build_spec function."""

    def test_normal_repo_passes(self, tmp_project):
        """Normal repo state should have PASS results."""
        results = check_build_spec(tmp_project)

        statuses = {r.status for r in results}
        # Should have PASS for manifest and hiddenimports
        assert "FAIL" not in statuses

    def test_missing_manifest_returns_fail(self, tmp_project_missing_manifest):
        """Missing manifest file should return FAIL."""
        # Create build.spec referencing manifest
        (tmp_project_missing_manifest / "build.spec").write_text(
            "manifest='build/app.manifest'\n"
            "hiddenimports=['core.windows_bootstrap']\n"
        )

        results = check_build_spec(tmp_project_missing_manifest)
        statuses = {r.status for r in results}

        assert "FAIL" in statuses
        assert any("app.manifest" in r.message and "FAIL" in r.status for r in results)

    def test_commented_icon_returns_warn(self, tmp_project):
        """Commented icon should only return WARN, not FAIL."""
        (tmp_project / "build.spec").write_text(
            "a = Analysis(['main.py'])\n"
            "manifest='build/app.manifest'\n"
            "hiddenimports=['core.windows_bootstrap']\n"
            "exe = EXE(pyz, [], name='App',\n"
            "          # icon='app.ico',\n"
            "          )\n"
        )

        results = check_build_spec(tmp_project)
        statuses = {r.status for r in results}

        # Icon warn is OK - should not fail
        assert "FAIL" not in statuses
        assert any(r.status == "WARN" and "icon" in r.message for r in results)

    def test_missing_windows_bootstrap_hiddenimport_returns_fail(self, tmp_project):
        """Missing windows_bootstrap in hiddenimports should FAIL."""
        (tmp_project / "build.spec").write_text(
            "exe = EXE(pyz, [], name='App', manifest='build/app.manifest')\n"
        )

        results = check_build_spec(tmp_project)
        statuses = {r.status for r in results}

        assert "FAIL" in statuses

    def test_unsupported_specdir_global_returns_fail(self, tmp_project):
        """PyInstaller no longer provides SPECDIR in the spec namespace."""
        (tmp_project / "build.spec").write_text(
            "from pathlib import Path\n"
            "root = Path(SPECDIR).absolute()\n"
            "manifest='build/app.manifest'\n"
            "hiddenimports=['core.windows_bootstrap']\n"
        )

        results = check_build_spec(tmp_project)

        assert any(
            r.status == "FAIL" and "SPECDIR" in r.message
            for r in results
        )

    def test_onedir_collect_without_exclude_binaries_returns_fail(self, tmp_project):
        """Directory builds should keep binaries for COLLECT, not pack them into EXE."""
        (tmp_project / "build.spec").write_text(
            "manifest='build/app.manifest'\n"
            "hiddenimports=['core.windows_bootstrap']\n"
            "exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [])\n"
            "coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas)\n"
        )

        results = check_build_spec(tmp_project)

        assert any(
            r.status == "FAIL" and "exclude_binaries=True" in r.message
            for r in results
        )

    def test_onedir_collect_with_exclude_binaries_passes(self, tmp_project):
        """The release build should use PyInstaller's standard onedir shape."""
        (tmp_project / "build.spec").write_text(
            "manifest='build/app.manifest'\n"
            "hiddenimports=['core.windows_bootstrap']\n"
            "exe = EXE(pyz, a.scripts, [], exclude_binaries=True)\n"
            "coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas)\n"
        )

        results = check_build_spec(tmp_project)

        assert not any(
            r.status == "FAIL" and "exclude_binaries=True" in r.message
            for r in results
        )

    def test_tree_entries_in_analysis_datas_return_fail(self, tmp_project):
        """Tree() produces a TOC and should not be placed inside Analysis datas."""
        (tmp_project / "build.spec").write_text(
            "manifest='build/app.manifest'\n"
            "hiddenimports=['core.windows_bootstrap']\n"
            "datas = [Tree('assets', prefix='assets')]\n"
            "exe = EXE(pyz, a.scripts, [], exclude_binaries=True)\n"
            "coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas)\n"
        )

        results = check_build_spec(tmp_project)

        assert any(
            r.status == "FAIL" and "Tree entries" in r.message
            for r in results
        )


class TestCheckBootstrapOrder:
    """Tests for check_bootstrap_order function."""

    def test_normal_repo_passes(self, tmp_project):
        """Normal repo with correct order should PASS."""
        results = check_bootstrap_order(tmp_project)
        statuses = {r.status for r in results}

        assert "FAIL" not in statuses

    def test_main_py_missing_bootstrap_fails(self, tmp_path):
        """main.py missing bootstrap should FAIL."""
        core_dir = tmp_path / "core"
        core_dir.mkdir()
        (core_dir / "windows_bootstrap.py").write_text("# stub\n")

        (tmp_path / "main.py").write_text(
            "import sys\n"
            "from core import load_config\n"
        )

        results = check_bootstrap_order(tmp_path)
        statuses = {r.status for r in results}

        assert "FAIL" in statuses
        assert any("main.py" in r.message and "FAIL" in r.status for r in results)

    def test_web_desktop_gui_before_bootstrap_fails(self, tmp_project_gui_before_bootstrap):
        """GUI import before bootstrap should FAIL."""
        results = check_bootstrap_order(tmp_project_gui_before_bootstrap)
        statuses = {r.status for r in results}

        assert "FAIL" in statuses
        assert any(
            "GUI import" in r.message and "FAIL" in r.status
            for r in results
        )

    def test_web_backend_main_entry_has_bootstrap(self, tmp_project):
        """web_backend.py __main__ should have bootstrap."""
        results = check_bootstrap_order(tmp_project)
        web_backend_results = [
            r for r in results if "web_backend" in r.message
        ]

        assert any("PASS" in r.status for r in web_backend_results)


class TestCheckImportSmoke:
    """Tests for check_import_smoke function."""

    def test_stub_modules_import_ok(self, tmp_project):
        """Stub modules should import without errors."""
        with patch("sys.path", sys.path + [str(tmp_project)]):
            results = check_import_smoke(tmp_project)
            # Stubs may fail if not complete, but should not crash
            assert len(results) == 4  # 4 modules checked

    def test_results_have_proper_status(self):
        """Results should have valid status values."""
        # Without root, this will fail imports
        results = check_import_smoke()

        for result in results:
            assert result.status in ("PASS", "WARN", "FAIL")
            assert hasattr(result, "category")
            assert hasattr(result, "message")


class TestRunChecks:
    """Integration tests for run_checks function."""

    def test_normal_repo_returns_zero_exit_code(self, tmp_project):
        """Normal repo should return exit code 0."""
        exit_code, results = run_checks(tmp_project)

        assert exit_code == 0
        assert len(results) > 0

    def test_missing_manifest_returns_nonzero_exit_code(self, tmp_project_missing_manifest):
        """Missing manifest should return exit code 1."""
        (tmp_project_missing_manifest / "build.spec").write_text(
            "manifest='build/app.manifest'\n"
            "hiddenimports=['core.windows_bootstrap']\n"
        )

        exit_code, results = run_checks(tmp_project_missing_manifest)

        assert exit_code == 1
        assert any(r.status == "FAIL" for r in results)

    def test_only_warnings_returns_zero(self, tmp_path):
        """Only warnings should return exit code 0."""
        core_dir = tmp_path / "core"
        core_dir.mkdir()
        (core_dir / "windows_bootstrap.py").write_text("# stub\n")
        (core_dir / "diagnostic_events.py").write_text("# stub\n")
        (core_dir / "screenshot_cleanup.py").write_text("# stub\n")
        (core_dir / "cloud_outbox.py").write_text("# stub\n")

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / "app.manifest").write_text("<?xml version='1.0'?>\n<assembly/>")

        (tmp_path / "build.spec").write_text(
            "manifest='build/app.manifest'\n"
            "hiddenimports=['core.windows_bootstrap']\n"
            "# icon='app.ico'\n"
        )

        (tmp_path / "main.py").write_text(
            "from core.windows_bootstrap import install_windows_bootstrap\n"
            "install_windows_bootstrap()\n"
        )

        (tmp_path / "web_desktop.py").write_text(
            "from core.windows_bootstrap import install_windows_bootstrap\n"
            "install_windows_bootstrap()\n"
        )

        (tmp_path / "web_backend.py").write_text(
            "if __name__ == '__main__':\n"
            "    from core.windows_bootstrap import install_windows_bootstrap\n"
            "    install_windows_bootstrap()\n"
        )

        exit_code, results = run_checks(tmp_path)

        # Should have only PASS and WARN, no FAIL
        statuses = {r.status for r in results}
        assert "FAIL" not in statuses
        assert exit_code == 0


class TestCheckResultNamedTuple:
    """Tests for CheckResult namedtuple."""

    def test_check_result_fields(self):
        """CheckResult should have status, category, message fields."""
        result = CheckResult("PASS", "test.category", "Test message")

        assert result.status == "PASS"
        assert result.category == "test.category"
        assert result.message == "Test message"

    def test_check_result_immutable(self):
        """CheckResult fields should be immutable."""
        result = CheckResult("PASS", "test", "msg")

        with pytest.raises(AttributeError):
            result.status = "FAIL"  # type: ignore
