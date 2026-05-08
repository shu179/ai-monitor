from __future__ import annotations

import unittest
import requests

from core.cloud_client import CloudClientError, SurfacedCloudClient, iter_sse_events


class FakeResponse:
    status_code = 200
    content = b'{"id": 7, "config_version": 4}'
    text = '{"id": 7, "config_version": 4}'

    def json(self):
        return {"id": 7, "config_version": 4}


class FakeErrorResponse:
    status_code = 409
    text = '{"detail":{"message":"账号名已被使用，建议使用张三01","suggested_username":"张三01"}}'
    content = text.encode("utf-8")

    def json(self):
        return {
            "detail": {
                "message": "账号名已被使用，建议使用张三01",
                "suggested_username": "张三01",
            }
        }


class FakeValidationErrorResponse:
    status_code = 422
    text = '{"detail":[{"type":"string_too_long","loc":["query","last_event_id"],"msg":"String should have at most 256 characters","ctx":{"max_length":256}}]}'
    content = text.encode("utf-8")

    def json(self):
        return {
            "detail": [
                {
                    "type": "string_too_long",
                    "loc": ["query", "last_event_id"],
                    "msg": "String should have at most 256 characters",
                    "ctx": {"max_length": 256},
                }
            ]
        }


class FakeStreamResponse:
    status_code = 200
    content = b""
    text = ""

    def iter_lines(self, decode_unicode: bool = False):
        del decode_unicode
        return iter(["event: heartbeat", "data: {}", ""])

    def close(self):
        pass


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()


class FakeErrorSession(FakeSession):
    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeErrorResponse()


class FakeValidationErrorSession(FakeSession):
    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeValidationErrorResponse()


class FakeStreamSession(FakeSession):
    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeStreamResponse()


class FakeTimeoutSession(FakeSession):
    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        raise requests.ReadTimeout("HTTPSConnectionPool(host='api.example.com', port=443): Read timed out.")


class CloudClientTests(unittest.TestCase):
    def test_update_admin_task_uses_patch_and_bearer_token(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        result = client.update_admin_task("access-token", 7, {"name": "新任务", "expected_config_version": 3})

        self.assertEqual(result["config_version"], 4)
        self.assertEqual(session.calls[0]["method"], "PATCH")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/admin/tasks/7")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(session.calls[0]["json"], {"name": "新任务", "expected_config_version": 3})

    def test_error_detail_dict_uses_message_text(self):
        session = FakeErrorSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        with self.assertRaises(CloudClientError) as caught:
            client.create_admin_user("access-token", {"username": "张三"})

        self.assertEqual(str(caught.exception), "账号名已被使用，建议使用张三01")

    def test_validation_error_detail_list_is_summarized(self):
        session = FakeValidationErrorSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        with self.assertRaises(CloudClientError) as caught:
            list(client.stream_events("access-token"))

        self.assertEqual(str(caught.exception), "云端参数校验失败：last_event_id 超出长度限制")

    def test_stream_events_skips_oversized_last_event_id_query(self):
        session = FakeStreamSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        events = list(client.stream_events("access-token", last_event_id="x" * 300))

        self.assertEqual(events[0]["event"], "heartbeat")
        self.assertIsNone(session.calls[0]["params"])

    def test_request_timeout_error_is_short_and_user_readable(self):
        session = FakeTimeoutSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        with self.assertRaises(CloudClientError) as caught:
            client.list_tasks("access-token")

        self.assertEqual(str(caught.exception), "云端请求超时，将自动重试")

    def test_event_stream_timeout_error_is_short_and_user_readable(self):
        session = FakeTimeoutSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        with self.assertRaises(CloudClientError) as caught:
            list(client.stream_events("access-token"))

        self.assertEqual(str(caught.exception), "云端事件连接超时，正在自动重连")

    def test_register_admin_posts_public_auth_payload(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.register_admin(
            email="admin@example.com",
            password="Password123",
            workspace_name="示例工作区",
            display_name="管理员",
        )

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/auth/admin/register")
        self.assertNotIn("Authorization", session.calls[0]["headers"])
        self.assertEqual(
            session.calls[0]["json"],
            {
                "email": "admin@example.com",
                "password": "Password123",
                "workspace_name": "示例工作区",
                "display_name": "管理员",
            },
        )

    def test_verify_email_posts_code_and_device_info(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.verify_email(
            email="admin@example.com",
            code="123456",
            device_id="device-1",
            app_version="0.1.0",
        )

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/auth/email/verify")
        self.assertEqual(
            session.calls[0]["json"],
            {
                "email": "admin@example.com",
                "code": "123456",
                "device_id": "device-1",
                "app_version": "0.1.0",
            },
        )

    def test_resend_email_verification_posts_email(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.resend_email_verification(email="admin@example.com")

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/auth/email/resend")
        self.assertEqual(session.calls[0]["json"], {"email": "admin@example.com"})

    def test_request_password_reset_posts_email(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.request_password_reset(email="admin@example.com")

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/auth/password/reset/request")
        self.assertEqual(session.calls[0]["json"], {"email": "admin@example.com"})

    def test_reset_password_posts_code_and_new_password(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.reset_password(email="admin@example.com", code="123456", password="NewPassword123")

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/auth/password/reset/confirm")
        self.assertEqual(
            session.calls[0]["json"],
            {"email": "admin@example.com", "code": "123456", "password": "NewPassword123"},
        )

    def test_clear_admin_task_operator_uses_delete(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        result = client.clear_admin_task_operator("access-token", 7)

        self.assertEqual(result["id"], 7)
        self.assertEqual(session.calls[0]["method"], "DELETE")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/admin/tasks/7/members/operator")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")

    def test_delete_admin_task_uses_delete(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.delete_admin_task("access-token", 7)

        self.assertEqual(session.calls[0]["method"], "DELETE")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/admin/tasks/7")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")

    def test_restore_admin_task_posts_restore(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.restore_admin_task("access-token", 7)

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/admin/tasks/7/restore")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")

    def test_list_deleted_tasks_uses_tasks_deleted_endpoint(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.list_deleted_tasks("access-token")

        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/tasks/deleted")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")

    def test_task_run_records_supports_since_id_cursor(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.task_run_records("access-token", 7, limit=6000, since_id=12)

        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/tasks/7/run-records")
        self.assertEqual(session.calls[0]["params"], {"limit": 6000, "since_id": 12})

    def test_iter_sse_events_parses_named_json_event(self):
        events = list(
            iter_sse_events(
                [
                    "id: 42",
                    "event: task_changed",
                    'data: {"task_id":7}',
                    "",
                ]
            )
        )

        self.assertEqual(events, [{"event": "task_changed", "data": {"task_id": 7}, "id": "42", "raw_data": '{"task_id":7}'}])


if __name__ == "__main__":
    unittest.main()
