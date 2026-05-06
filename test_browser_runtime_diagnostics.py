import unittest

from core.browser_auth import build_browser_runtime_diagnostics


class BrowserRuntimeDiagnosticsTests(unittest.TestCase):
    def test_flags_shared_profile_direct_ip_and_external_cdp(self):
        snapshots = {
            "deepseek": {
                "active_profile_id": "default",
                "active_profile": {
                    "id": "default",
                    "absolute_path": "/tmp/shared-profile",
                    "authenticated": True,
                    "auth_state": "authenticated",
                    "last_runtime_binding": {
                        "account_profile_dir": "/tmp/account-a",
                        "user_data_dir": "/tmp/shared-profile",
                        "proxy_server": "",
                        "use_external_chrome_cdp": True,
                        "external_chrome_light_control": True,
                    },
                },
            },
            "kimi": {
                "active_profile_id": "default",
                "active_profile": {
                    "id": "default",
                    "absolute_path": "/tmp/shared-profile",
                    "authenticated": True,
                    "auth_state": "authenticated",
                    "last_runtime_binding": {
                        "account_profile_dir": "/tmp/account-a",
                        "user_data_dir": "/tmp/shared-profile",
                        "proxy_server": "",
                        "use_external_chrome_cdp": True,
                        "external_chrome_light_control": True,
                    },
                },
            },
        }

        diagnostics = build_browser_runtime_diagnostics(snapshots)
        issue_ids = {issue["id"] for issue in diagnostics["issues"]}

        self.assertEqual(diagnostics["status"], "high_risk")
        self.assertIn("shared_user_data_dir", issue_ids)
        self.assertIn("shared_direct_ip", issue_ids)
        self.assertIn("external_cdp_port_mode", issue_ids)
        self.assertGreaterEqual(diagnostics["summary"]["externalCdpPlatforms"], 2)

    def test_flags_shared_proxy_without_profile_collision(self):
        snapshots = {
            "deepseek": {
                "active_profile": {
                    "id": "default",
                    "absolute_path": "/tmp/deepseek",
                    "last_runtime_binding": {
                        "account_profile_dir": "/tmp/account-a",
                        "user_data_dir": "/tmp/deepseek",
                        "proxy_server": "http://proxy.example:8080",
                        "use_external_chrome_cdp": False,
                    },
                },
            },
            "tongyi": {
                "active_profile": {
                    "id": "default",
                    "absolute_path": "/tmp/tongyi",
                    "last_runtime_binding": {
                        "account_profile_dir": "/tmp/account-a",
                        "user_data_dir": "/tmp/tongyi",
                        "proxy_server": "http://proxy.example:8080",
                        "use_external_chrome_cdp": False,
                    },
                },
            },
        }

        diagnostics = build_browser_runtime_diagnostics(snapshots)
        issue_ids = {issue["id"] for issue in diagnostics["issues"]}

        self.assertIn("shared_proxy_server", issue_ids)
        self.assertNotIn("shared_user_data_dir", issue_ids)

    def test_missing_runtime_binding_is_not_treated_as_direct_ip(self):
        snapshots = {
            "deepseek": {
                "active_profile": {
                    "id": "default",
                    "absolute_path": "/tmp/deepseek",
                    "last_runtime_binding": {},
                },
            },
            "kimi": {
                "active_profile": {
                    "id": "default",
                    "absolute_path": "/tmp/kimi",
                    "last_runtime_binding": {},
                },
            },
        }

        diagnostics = build_browser_runtime_diagnostics(snapshots)
        issue_ids = {issue["id"] for issue in diagnostics["issues"]}

        self.assertIn("missing_recent_runtime_binding", issue_ids)
        self.assertNotIn("shared_direct_ip", issue_ids)
        self.assertEqual(diagnostics["summary"]["directIpPlatforms"], 0)


if __name__ == "__main__":
    unittest.main()
