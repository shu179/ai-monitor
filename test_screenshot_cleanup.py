"""Tests for core.screenshot_cleanup."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.screenshot_cleanup import (
    DEFAULT_SCREENSHOT_CLEANUP_CONFIG,
    IMAGE_EXTENSIONS,
    cleanup_screenshots,
    cleanup_screenshots_from_default_config,
)


def _make_file(path: Path, size: int = 100, mtime: float | None = None) -> Path:
    """Create a file with given size and mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class DefaultConfigTests(unittest.TestCase):
    def test_missing_screenshot_key_enables_cleanup(self):
        result = cleanup_screenshots({})
        self.assertTrue(result["enabled"])

    def test_missing_auto_cleanup_defaults_true(self):
        result = cleanup_screenshots({"screenshot": {"retention_days": 7}})
        self.assertTrue(result["enabled"])

    def test_explicit_auto_cleanup_false_disables(self):
        result = cleanup_screenshots({"screenshot": {"auto_cleanup": False}})
        self.assertFalse(result["enabled"])
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["scanned"], 0)

    def test_default_config_values(self):
        self.assertTrue(DEFAULT_SCREENSHOT_CLEANUP_CONFIG["auto_cleanup"])
        self.assertEqual(DEFAULT_SCREENSHOT_CLEANUP_CONFIG["retention_days"], 14)
        self.assertEqual(DEFAULT_SCREENSHOT_CLEANUP_CONFIG["max_size_mb"], 1024)
        self.assertTrue(DEFAULT_SCREENSHOT_CLEANUP_CONFIG["include_recognition"])

    def test_bad_retention_days_uses_default(self):
        """Non-numeric retention_days falls back to default without raising."""
        result = cleanup_screenshots({"screenshot": {"retention_days": "bad", "auto_cleanup": True}})
        self.assertTrue(result["enabled"])

    def test_bad_max_size_mb_uses_default(self):
        """Non-numeric max_size_mb falls back to default without raising."""
        result = cleanup_screenshots({"screenshot": {"max_size_mb": "", "auto_cleanup": True}})
        self.assertTrue(result["enabled"])


class AgeCleanupTests(unittest.TestCase):
    def test_old_jpg_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            old_time = time.time() - 20 * 86400  # 20 days ago
            _make_file(screenshots_dir / "old.jpg", size=100, mtime=old_time)

            result = cleanup_screenshots(
                {"screenshot": {"retention_days": 14, "auto_cleanup": True, "max_size_mb": 1024}},
                screenshots_dir=screenshots_dir,
            )
            self.assertEqual(result["deleted"], 1)
            self.assertEqual(result["reason_counts"]["age"], 1)
            self.assertFalse((screenshots_dir / "old.jpg").exists())

    def test_new_jpg_kept(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            recent_time = time.time() - 1 * 86400  # 1 day ago
            _make_file(screenshots_dir / "new.jpg", size=100, mtime=recent_time)

            result = cleanup_screenshots(
                {"screenshot": {"retention_days": 14, "auto_cleanup": True, "max_size_mb": 1024}},
                screenshots_dir=screenshots_dir,
            )
            self.assertEqual(result["deleted"], 0)
            self.assertEqual(result["kept"], 1)
            self.assertTrue((screenshots_dir / "new.jpg").exists())


class SizeCleanupTests(unittest.TestCase):
    def test_size_under_limit_keeps_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            now = time.time()
            _make_file(screenshots_dir / "old.jpg", size=100, mtime=now - 30 * 86400)
            _make_file(screenshots_dir / "mid.jpg", size=100, mtime=now - 20 * 86400)
            _make_file(screenshots_dir / "new.jpg", size=100, mtime=now - 1 * 86400)

            result = cleanup_screenshots(
                {"screenshot": {"retention_days": 365, "auto_cleanup": True, "max_size_mb": 1024}},
                screenshots_dir=screenshots_dir,
                now=now,
            )
            self.assertEqual(result["deleted"], 0)

    def test_size_limit_deletes_oldest_first(self):
        """With max_bytes=0 (patched min), all files are over limit; oldest deleted first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            now = time.time()
            _make_file(screenshots_dir / "old.jpg", size=500, mtime=now - 30 * 86400)
            _make_file(screenshots_dir / "new.jpg", size=500, mtime=now - 1 * 86400)

            with patch("core.screenshot_cleanup._MIN_MAX_SIZE_MB", 0):
                result = cleanup_screenshots(
                    {"screenshot": {"retention_days": 365, "auto_cleanup": True, "max_size_mb": 0}},
                    screenshots_dir=screenshots_dir,
                    now=now,
                )
            self.assertEqual(result["deleted"], 2)
            self.assertEqual(result["reason_counts"]["size"], 2)


class ExtensionTests(unittest.TestCase):
    def test_jpeg_and_png_recognized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            old_time = time.time() - 20 * 86400
            _make_file(screenshots_dir / "photo.jpeg", size=100, mtime=old_time)
            _make_file(screenshots_dir / "screen.png", size=100, mtime=old_time)
            _make_file(screenshots_dir / "upper.JPG", size=100, mtime=old_time)
            _make_file(screenshots_dir / "upper.JPEG", size=100, mtime=old_time)
            _make_file(screenshots_dir / "upper.PNG", size=100, mtime=old_time)

            result = cleanup_screenshots(
                {"screenshot": {"retention_days": 14, "auto_cleanup": True, "max_size_mb": 1024}},
                screenshots_dir=screenshots_dir,
            )
            self.assertEqual(result["deleted"], 5)
            self.assertEqual(result["scanned"], 5)

    def test_non_image_files_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            old_time = time.time() - 20 * 86400
            _make_file(screenshots_dir / "notes.txt", size=100, mtime=old_time)
            _make_file(screenshots_dir / "data.json", size=100, mtime=old_time)
            _make_file(screenshots_dir / "noext", size=100, mtime=old_time)
            _make_file(screenshots_dir / "old.jpg", size=100, mtime=old_time)

            result = cleanup_screenshots(
                {"screenshot": {"retention_days": 14, "auto_cleanup": True, "max_size_mb": 1024}},
                screenshots_dir=screenshots_dir,
            )
            self.assertEqual(result["deleted"], 1)
            self.assertTrue((screenshots_dir / "notes.txt").exists())
            self.assertTrue((screenshots_dir / "data.json").exists())
            self.assertTrue((screenshots_dir / "noext").exists())


class RecognitionSubdirTests(unittest.TestCase):
    def test_recognition_subdir_cleaned_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            old_time = time.time() - 20 * 86400
            _make_file(screenshots_dir / "recognition" / "old.jpg", size=100, mtime=old_time)
            _make_file(screenshots_dir / "other.jpg", size=100, mtime=old_time)

            result = cleanup_screenshots(
                {"screenshot": {"retention_days": 14, "auto_cleanup": True, "max_size_mb": 1024}},
                screenshots_dir=screenshots_dir,
            )
            self.assertEqual(result["deleted"], 2)
            self.assertFalse((screenshots_dir / "recognition" / "old.jpg").exists())

    def test_recognition_subdir_skipped_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            old_time = time.time() - 20 * 86400
            _make_file(screenshots_dir / "recognition" / "old.jpg", size=100, mtime=old_time)
            _make_file(screenshots_dir / "root.jpg", size=100, mtime=old_time)

            result = cleanup_screenshots(
                {"screenshot": {"retention_days": 14, "auto_cleanup": True, "max_size_mb": 1024, "include_recognition": False}},
                screenshots_dir=screenshots_dir,
            )
            self.assertEqual(result["deleted"], 1)
            self.assertTrue((screenshots_dir / "recognition" / "old.jpg").exists())
            self.assertFalse((screenshots_dir / "root.jpg").exists())


class SymlinkTests(unittest.TestCase):
    def test_symlinks_not_followed(self):
        if not hasattr(os, "symlink"):
            self.skipTest("os.symlink not available")
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir) / "screenshots"
            screenshots_dir.mkdir()
            # Place target OUTSIDE screenshots_dir so rglob doesn't find it
            external_dir = Path(tmpdir) / "external"
            external_dir.mkdir()
            target = _make_file(external_dir / "target.jpg", size=100, mtime=time.time() - 20 * 86400)
            link = screenshots_dir / "link.jpg"
            try:
                os.symlink(target, link)
            except OSError:
                self.skipTest("symlink creation failed")

            result = cleanup_screenshots(
                {"screenshot": {"retention_days": 14, "auto_cleanup": True, "max_size_mb": 1024}},
                screenshots_dir=screenshots_dir,
            )
            # Symlink should be skipped, not deleted
            self.assertEqual(result["deleted"], 0)
            self.assertTrue(link.is_symlink())
            # External target should be untouched
            self.assertTrue(target.exists())


class ErrorHandlingTests(unittest.TestCase):
    def test_missing_directory_returns_empty_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = cleanup_screenshots(
                {"screenshot": {"auto_cleanup": True}},
                screenshots_dir=Path(tmpdir) / "nonexistent",
            )
            self.assertTrue(result["enabled"])
            self.assertEqual(result["scanned"], 0)
            self.assertEqual(result["deleted"], 0)

    def test_delete_failure_does_not_stop_others(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            old_time = time.time() - 20 * 86400
            _make_file(screenshots_dir / "a.jpg", size=100, mtime=old_time)
            _make_file(screenshots_dir / "b.jpg", size=100, mtime=old_time)

            original_unlink = Path.unlink
            call_count = [0]

            def failing_unlink(self, *args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise PermissionError("mocked permission denied")
                return original_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", failing_unlink):
                result = cleanup_screenshots(
                    {"screenshot": {"retention_days": 14, "auto_cleanup": True, "max_size_mb": 1024}},
                    screenshots_dir=screenshots_dir,
                )
            # First delete fails, second succeeds
            self.assertEqual(result["deleted"], 1)
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("permission denied", result["errors"][0]["error"].lower())


class DefaultConfigWrapperTests(unittest.TestCase):
    def test_load_config_failure_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)
            old_time = time.time() - 20 * 86400
            _make_file(screenshots_dir / "old.jpg", size=100, mtime=old_time)

            with patch("core.screenshot_cleanup.cleanup_screenshots") as mock_cleanup:
                mock_cleanup.return_value = {"enabled": True}
                with patch("core.config_watcher.load_config", side_effect=Exception("no config")):
                    result = cleanup_screenshots_from_default_config()
            # Should have called cleanup_screenshots with empty config (defaults)
            mock_cleanup.assert_called_once()
            called_config = mock_cleanup.call_args[0][0]
            self.assertEqual(called_config, {})


class SummaryStructureTests(unittest.TestCase):
    def test_result_has_all_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = cleanup_screenshots(
                {"screenshot": {"auto_cleanup": True}},
                screenshots_dir=Path(tmpdir),
            )
            self.assertIn("enabled", result)
            self.assertIn("directory", result)
            self.assertIn("deleted", result)
            self.assertIn("deleted_bytes", result)
            self.assertIn("kept", result)
            self.assertIn("scanned", result)
            self.assertIn("reason_counts", result)
            self.assertIn("errors", result)
            self.assertIn("age", result["reason_counts"])
            self.assertIn("size", result["reason_counts"])


if __name__ == "__main__":
    unittest.main()
