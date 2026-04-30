import base64
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import core.update_manager as update_manager


def _version_payload():
    return {
        "appName": "Surfaced",
        "slug": "surfaced",
        "version": "2026.04.01",
        "channel": "stable",
        "label": "v2026.04.01",
        "full": "Surfaced v2026.04.01",
    }


def _write_manifest(directory: str, payload: dict) -> tuple[Path, bytes]:
    path = Path(directory) / "manifest.json"
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return path, raw


class UpdateManagerSecurityTests(unittest.TestCase):
    def _build_status(self, config: dict) -> dict:
        with patch.object(update_manager, "get_version_payload", side_effect=_version_payload), patch.object(
            update_manager,
            "get_platform_package_keys",
            return_value=["darwin-arm64", "darwin"],
        ):
            return update_manager.build_update_status(config, include_check=True)

    def test_unsigned_manifest_remains_compatible_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path, _ = _write_manifest(
                tmp,
                {
                    "version": "2026.04.20",
                    "channel": "stable",
                    "packages": {
                        "darwin-arm64": "https://example.com/Surfaced.zip",
                    },
                },
            )

            result = self._build_status({"app_update": {"manifest_url": str(manifest_path)}})

        self.assertTrue(result["ok"])
        self.assertTrue(result["update_available"])
        self.assertEqual(result["download_url"], "https://example.com/Surfaced.zip")
        self.assertFalse(result["security"]["manifest_signature_required"])

    def test_manifest_sha256_pin_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path, raw = _write_manifest(
                tmp,
                {
                    "version": "2026.04.20",
                    "channel": "stable",
                    "download_url": "https://example.com/Surfaced.zip",
                },
            )
            manifest_hash = hashlib.sha256(raw).hexdigest()

            result = self._build_status({
                "app_update": {
                    "manifest_url": str(manifest_path),
                    "manifest_sha256": manifest_hash,
                }
            })

        self.assertTrue(result["ok"])
        self.assertTrue(result["security"]["manifest_hash_checked"])
        self.assertEqual(result["security"]["manifest_sha256"], manifest_hash)

    def test_manifest_sha256_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path, _ = _write_manifest(
                tmp,
                {
                    "version": "2026.04.20",
                    "download_url": "https://example.com/Surfaced.zip",
                },
            )

            result = self._build_status({
                "app_update": {
                    "manifest_url": str(manifest_path),
                    "manifest_sha256": "1" * 64,
                }
            })

        self.assertFalse(result["ok"])
        self.assertIn("sha256", result["message"])

    def test_signed_manifest_envelope_is_verified_and_unwrapped(self):
        signed_payload = {
            "version": "2026.04.20",
            "channel": "stable",
            "packages": {
                "darwin-arm64": {
                    "url": "https://example.com/Surfaced.zip",
                    "sha256": "2" * 64,
                },
            },
        }
        signature = base64.b64encode(b"signature").decode("ascii")
        manifest = {
            "signed": signed_payload,
            "signature": {
                "algorithm": "ed25519",
                "key_id": "release-key-1",
                "value": signature,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path, _ = _write_manifest(tmp, manifest)
            with patch.object(update_manager, "_verify_signature_bytes") as verify:
                result = self._build_status({
                    "app_update": {
                        "manifest_url": str(manifest_path),
                        "manifest_public_key": "public-key",
                        "require_signature": True,
                        "require_package_hash": True,
                    }
                })

        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"]["version"], "2026.04.20")
        self.assertEqual(result["package_sha256"], "2" * 64)
        self.assertTrue(result["security"]["manifest_signature_checked"])
        self.assertEqual(result["security"]["signature_key_id"], "release-key-1")
        verify.assert_called_once()
        self.assertEqual(verify.call_args.kwargs["canonical_payload"], update_manager._canonical_json_bytes(signed_payload))
        self.assertEqual(verify.call_args.kwargs["signature"], b"signature")

    def test_signature_failure_rejects_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path, _ = _write_manifest(
                tmp,
                {
                    "version": "2026.04.20",
                    "signature": {
                        "algorithm": "ed25519",
                        "value": base64.b64encode(b"bad").decode("ascii"),
                    },
                },
            )
            with patch.object(update_manager, "_verify_signature_bytes", side_effect=ValueError("bad signature")):
                result = self._build_status({
                    "app_update": {
                        "manifest_url": str(manifest_path),
                        "manifest_public_key": "public-key",
                        "require_signature": True,
                    }
                })

        self.assertFalse(result["ok"])
        self.assertIn("bad signature", result["message"])

    def test_package_hash_can_be_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_hash_path, _ = _write_manifest(
                tmp,
                {
                    "version": "2026.04.20",
                    "packages": {"darwin-arm64": "https://example.com/Surfaced.zip"},
                },
            )
            missing_hash_result = self._build_status({
                "app_update": {
                    "manifest_url": str(missing_hash_path),
                    "require_package_hash": True,
                }
            })

            hash_path, _ = _write_manifest(
                tmp,
                {
                    "version": "2026.04.20",
                    "packages": {
                        "darwin-arm64": {
                            "url": "https://example.com/Surfaced.zip",
                            "sha256": "3" * 64,
                        },
                    },
                },
            )
            hash_result = self._build_status({
                "app_update": {
                    "manifest_url": str(hash_path),
                    "require_package_hash": True,
                }
            })

        self.assertFalse(missing_hash_result["ok"])
        self.assertIn("安装包 sha256", missing_hash_result["message"])
        self.assertTrue(hash_result["ok"])
        self.assertEqual(hash_result["package_sha256"], "3" * 64)

    def test_verify_file_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.zip"
            path.write_bytes(b"payload")
            expected = hashlib.sha256(b"payload").hexdigest()

            self.assertTrue(update_manager.verify_file_sha256(path, expected))
            with self.assertRaises(ValueError):
                update_manager.verify_file_sha256(path, "4" * 64)

    def test_prepare_update_package_copies_verifies_and_extracts_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "Surfaced.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("Surfaced-new/main.py", "print('ok')\n")
                bundle.writestr("Surfaced-new/web_backend.py", "")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()

            result = update_manager.prepare_update_package(
                download_url=str(archive),
                expected_sha256=digest,
                download_dir=tmp_path / "downloads",
                staging_dir=tmp_path / "staging",
            )

            self.assertEqual(result["sha256"], digest)
            self.assertTrue(Path(result["archive_path"]).exists())
            self.assertEqual(Path(result["source_dir"]).name, "Surfaced-new")
            self.assertTrue((Path(result["source_dir"]) / "main.py").exists())

    def test_prepare_update_package_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../evil.txt", "bad")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()

            with self.assertRaises(ValueError):
                update_manager.prepare_update_package(
                    download_url=str(archive),
                    expected_sha256=digest,
                    download_dir=tmp_path / "downloads",
                    staging_dir=tmp_path / "staging",
                )


if __name__ == "__main__":
    unittest.main()
