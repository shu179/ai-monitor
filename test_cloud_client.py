from __future__ import annotations

import unittest
import requests

from core.cloud_client import CloudClientError, SurfacedCloudClient, iter_sse_events, new_trace_id, normalize_trace_id


class FakeResponse:
    status_code = 200
    headers = {}
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


class FakeBackpressureResponse:
    status_code = 429
    headers = {
        "Retry-After": "12",
        "X-Queue-Depth-Hint": "23000",
        "X-Throttle-Bucket": "sync_metadata",
    }
    text = '{"detail":{"message":"queue overloaded"}}'
    content = text.encode("utf-8")

    def json(self):
        return {"detail": {"message": "queue overloaded"}}


class FakeStreamResponse:
    status_code = 200
    content = b""
    text = ""

    def iter_lines(self, decode_unicode: bool = False):
        del decode_unicode
        return iter(["event: heartbeat", "data: {}", ""])

    def close(self):
        pass


class FakeBinaryResponse:
    status_code = 200
    content = b""
    text = ""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = chunks or [b"object-", b"bytes"]
        self.closed = False

    def iter_content(self, chunk_size: int = 1024):
        del chunk_size
        return iter(self._chunks)

    def close(self):
        self.closed = True


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


class FakeBackpressureSession(FakeSession):
    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeBackpressureResponse()


class FakeStreamSession(FakeSession):
    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeStreamResponse()


class FakeBinarySession(FakeSession):
    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeBinaryResponse()


class FakeUploadSession(FakeSession):
    def __init__(self, *, response: FakeResponse | None = None) -> None:
        super().__init__()
        self.response = response or FakeResponse()

    def request(self, method: str, url: str, **kwargs):
        captured = {"method": method, "url": url, **kwargs}
        data = kwargs.get("data")
        if data is not None:
            captured["body"] = b"".join(data)
        self.calls.append(captured)
        return self.response


class FakeUploadPartResponse:
    status_code = 200
    headers = {"ETag": '"part-etag"'}
    content = b""
    text = ""

    def json(self):
        raise ValueError("no json")


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

    def test_request_error_carries_backpressure_headers(self):
        session = FakeBackpressureSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        with self.assertRaises(CloudClientError) as caught:
            client.post_events("access-token", [{"event_type": "run_record"}])

        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after_seconds, 12.0)
        self.assertEqual(caught.exception.queue_depth_hint, 23000)
        self.assertEqual(caught.exception.throttle_bucket, "sync_metadata")

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

    def test_admin_object_storage_report_uses_admin_ops_endpoint(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        result = client.admin_object_storage_report("access-token")

        self.assertEqual(result["id"], 7)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/admin/ops/object-storage")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")

    def test_admin_sync_queue_report_uses_admin_ops_endpoint(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        result = client.admin_sync_queue_report("access-token")

        self.assertEqual(result["id"], 7)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/admin/ops/sync-queue")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")

    def test_admin_requeue_sync_queue_posts_bounded_payload(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        result = client.admin_requeue_sync_queue(
            "access-token",
            workspace_id=7,
            statuses=["blocked"],
            partition_key="7:article:abc",
            limit=50,
            dry_run=False,
        )

        self.assertEqual(result["id"], 7)
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v1/admin/ops/sync-queue/requeue")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(
            session.calls[0]["json"],
            {
                "workspace_id": 7,
                "statuses": ["blocked"],
                "partition_key": "7:article:abc",
                "limit": 50,
                "dry_run": False,
            },
        )

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

    def test_post_events_forwards_trace_header(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.post_events("access-token", [], trace_id="outbox.trace_1!")

        self.assertEqual(session.calls[0]["headers"]["X-Trace-Id"], "outbox.trace_1")

    def test_capabilities_sends_v2_capability_header(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.capabilities("access-token", capabilities_header="sync-v2,state-delta-v1")

        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/capabilities")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(session.calls[0]["headers"]["X-Cloud-Capability"], "sync-v2,state-delta-v1")

    def test_state_delta_posts_v2_payload_and_headers(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.state_delta(
            "access-token",
            cursors={"tasks": 3, "articles": "7", "": 1},
            limit=1500,
            reset_token="reset-1",
            bootstrap_cursor="boot-1",
            device_id="device-a",
            priority=["articles"],
            trace_id="delta.trace!",
        )

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/sync/state-delta")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(session.calls[0]["headers"]["X-Trace-Id"], "delta.trace")
        self.assertEqual(session.calls[0]["headers"]["X-Cloud-Capability"], "sync-v2,batch-v2,object-v1,state-delta-v1")
        self.assertEqual(
            session.calls[0]["json"],
            {
                "device_id": "device-a",
                "cursors": {"tasks": 3, "articles": 7},
                "limit": 1000,
                "priority": ["articles"],
                "reset_token": "reset-1",
                "bootstrap_cursor": "boot-1",
            },
        )

    def test_create_object_download_posts_v2_request_and_headers(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.create_object_download("access-token", "object-1", trace_id="object.trace!")

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/objects/object-1:download")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(session.calls[0]["headers"]["X-Trace-Id"], "object.trace")
        self.assertEqual(session.calls[0]["headers"]["X-Cloud-Capability"], "sync-v2,batch-v2,object-v1,state-delta-v1")

    def test_create_object_upload_posts_v2_payload_and_headers(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.create_object_upload(
            "access-token",
            sha256="a" * 64,
            size_bytes=123,
            content_type="text/plain",
            storage_size_bytes=120,
            compression="zstd",
            trace_id="upload.trace!",
        )

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/objects/uploads")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(session.calls[0]["headers"]["X-Trace-Id"], "upload.trace")
        self.assertEqual(session.calls[0]["headers"]["X-Cloud-Capability"], "sync-v2,batch-v2,object-v1,state-delta-v1")
        self.assertEqual(
            session.calls[0]["json"],
            {
                "sha256": "a" * 64,
                "size_bytes": 123,
                "content_type": "text/plain",
                "storage_size_bytes": 120,
                "compression": "zstd",
            },
        )

    def test_upload_object_content_resolves_relative_url_with_authorization(self):
        session = FakeUploadSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.upload_object_content(
            "access-token",
            "/api/v2/objects/uploads/session-1/content",
            [b"hello", b"", b" world"],
            content_type="text/plain",
            headers={"X-Part": "1"},
            trace_id="upload.1",
        )

        self.assertEqual(session.calls[0]["method"], "PUT")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/objects/uploads/session-1/content")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(session.calls[0]["headers"]["Content-Type"], "text/plain")
        self.assertEqual(session.calls[0]["headers"]["X-Part"], "1")
        self.assertEqual(session.calls[0]["headers"]["X-Trace-Id"], "upload.1")
        self.assertEqual(session.calls[0]["body"], b"hello world")

    def test_upload_object_content_external_presigned_url_omits_authorization(self):
        session = FakeUploadSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.upload_object_content("access-token", "https://storage.example.com/upload?sig=1", [b"body"])

        self.assertEqual(session.calls[0]["url"], "https://storage.example.com/upload?sig=1")
        self.assertNotIn("Authorization", session.calls[0]["headers"])
        self.assertEqual(session.calls[0]["body"], b"body")

    def test_presign_object_upload_parts_posts_v2_request(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.presign_object_upload_parts("access-token", "session-1", [2, 1], trace_id="part.trace!")

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/objects/uploads/session-1/parts:presign")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(session.calls[0]["headers"]["X-Trace-Id"], "part.trace")
        self.assertEqual(session.calls[0]["headers"]["X-Cloud-Capability"], "sync-v2,batch-v2,object-v1,state-delta-v1")
        self.assertEqual(session.calls[0]["json"], {"part_numbers": [2, 1]})

    def test_upload_object_part_content_returns_etag_header(self):
        session = FakeUploadSession(response=FakeUploadPartResponse())
        client = SurfacedCloudClient("https://api.example.com", session=session)

        result = client.upload_object_part_content(
            "access-token",
            "https://storage.example.com/upload?partNumber=1",
            [b"part-body"],
            trace_id="part.1",
        )

        self.assertEqual(result["etag"], '"part-etag"')
        self.assertEqual(session.calls[0]["url"], "https://storage.example.com/upload?partNumber=1")
        self.assertNotIn("Authorization", session.calls[0]["headers"])
        self.assertEqual(session.calls[0]["body"], b"part-body")

    def test_record_object_upload_part_posts_v2_request(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.record_object_upload_part(
            "access-token",
            "session-1",
            part_number=3,
            etag='"etag-3"',
            size_bytes=99,
            sha256="b" * 64,
            trace_id="part.done",
        )

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/objects/uploads/session-1/parts")
        self.assertEqual(
            session.calls[0]["json"],
            {"part_number": 3, "etag": '"etag-3"', "size_bytes": 99, "sha256": "b" * 64},
        )

    def test_complete_object_upload_posts_v2_request(self):
        session = FakeSession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        client.complete_object_upload("access-token", "session-1", storage_size_bytes=123, compression="none")

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/objects/uploads/session-1:complete")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(
            session.calls[0]["json"],
            {"storage_size_bytes": 123, "compression": "none"},
        )

    def test_iter_object_content_resolves_relative_url_with_authorization(self):
        session = FakeBinarySession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        chunks = list(client.iter_object_content("access-token", "/api/v2/objects/object-1/content", trace_id="obj.1"))

        self.assertEqual(chunks, [b"object-", b"bytes"])
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://api.example.com/api/v2/objects/object-1/content")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(session.calls[0]["headers"]["X-Trace-Id"], "obj.1")
        self.assertTrue(session.calls[0]["stream"])

    def test_iter_object_content_external_presigned_url_omits_authorization(self):
        session = FakeBinarySession()
        client = SurfacedCloudClient("https://api.example.com", session=session)

        list(client.iter_object_content("access-token", "https://cdn.example.com/object?sig=1"))

        self.assertEqual(session.calls[0]["url"], "https://cdn.example.com/object?sig=1")
        self.assertNotIn("Authorization", session.calls[0]["headers"])

    def test_trace_id_helpers_sanitize_values(self):
        self.assertEqual(normalize_trace_id(" abc/def! "), "abcdef")
        self.assertTrue(new_trace_id("Outbox").startswith("outbox-"))

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
