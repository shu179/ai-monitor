from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from app.core.security import hash_password, verify_password  # noqa: E402
from app.models import User, UserRole  # noqa: E402
from app.services.admin_service import (  # noqa: E402
    create_workspace_user,
    list_workspace_users,
    update_workspace_user,
    workspace_user_public_payload,
)
from app.services.auth_service import login_user, update_my_profile  # noqa: E402


class UserAccountIdentityTests(unittest.TestCase):
    def test_create_workspace_user_preserves_chinese_username(self) -> None:
        db = Mock()
        db.scalar.return_value = None
        admin = SimpleNamespace(workspace_id=7)

        created = create_workspace_user(
            db,
            admin,  # type: ignore[arg-type]
            username=" 张三 ",
            password="Operator123456",
            role="operator",
            display_name=None,
            email=None,
            birthday=None,
            hire_date=None,
        )

        self.assertIsInstance(created, User)
        self.assertEqual(created.workspace_id, 7)
        self.assertEqual(created.username, "张三")
        self.assertEqual(created.display_name, "张三")
        self.assertEqual(created.role, UserRole.operator)
        self.assertTrue(verify_password("Operator123456", created.password_hash))
        db.add.assert_called_once_with(created)
        db.commit.assert_called_once()
        db.refresh.assert_called_once_with(created)

    def test_update_workspace_user_renames_username_and_display_name_together(self) -> None:
        user = SimpleNamespace(
            id=2,
            workspace_id=7,
            role=UserRole.operator,
            username="operator001",
            display_name="operator001",
            password_hash=hash_password("Operator123456"),
            token_version=1,
            enabled=True,
            birthday=None,
            hire_date=None,
        )
        db = Mock()
        db.scalar.side_effect = [user, None]
        admin = SimpleNamespace(workspace_id=7)

        updated = update_workspace_user(
            db,
            admin,  # type: ignore[arg-type]
            2,
            username=" 李四 ",
            password=None,
            display_name=None,
            email=None,
            birthday=None,
            hire_date=None,
            enabled=None,
            fields_set={"username"},
        )

        self.assertIs(updated, user)
        self.assertEqual(user.username, "李四")
        self.assertEqual(user.display_name, "李四")
        db.commit.assert_called_once()
        db.refresh.assert_called_once_with(user)

    def test_legacy_display_name_update_also_renames_ordinary_user(self) -> None:
        user = SimpleNamespace(
            id=2,
            workspace_id=7,
            role=UserRole.viewer,
            username="viewer001",
            display_name="viewer001",
            password_hash=hash_password("Viewer123456"),
            token_version=1,
            enabled=True,
            birthday=None,
            hire_date=None,
        )
        db = Mock()
        db.scalar.side_effect = [user, None]
        admin = SimpleNamespace(workspace_id=7)

        update_workspace_user(
            db,
            admin,  # type: ignore[arg-type]
            2,
            username=None,
            password=None,
            display_name=" 王五 ",
            email=None,
            birthday=None,
            hire_date=None,
            enabled=None,
            fields_set={"display_name"},
        )

        self.assertEqual(user.username, "王五")
        self.assertEqual(user.display_name, "王五")

    def test_create_workspace_user_conflict_recommends_available_username(self) -> None:
        db = Mock()
        db.scalar.side_effect = [5, 5, None]
        admin = SimpleNamespace(workspace_id=7)

        with self.assertRaises(HTTPException) as caught:
            create_workspace_user(
                db,
                admin,  # type: ignore[arg-type]
                username="张三",
                password="Operator123456",
                role="operator",
                display_name=None,
                email=None,
                birthday=None,
                hire_date=None,
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["suggested_username"], "张三02")
        self.assertIn("张三02", caught.exception.detail["message"])
        db.add.assert_not_called()

    def test_create_viewer_user_applies_visible_task_scope(self) -> None:
        db = Mock()
        db.scalar.return_value = None
        admin = SimpleNamespace(id=1, workspace_id=7)

        with (
            patch("app.services.admin_service._replace_viewer_visible_tasks") as replace_scope,
            patch("app.services.admin_service.record_workspace_change") as record_change,
        ):
            created = create_workspace_user(
                db,
                admin,  # type: ignore[arg-type]
                username="浏览员",
                password="Viewer123456",
                role="viewer",
                display_name=None,
                email=None,
                birthday=None,
                hire_date=None,
                view_all_tasks=True,
                visible_task_ids=[3, 4],
            )

        self.assertTrue(created.view_all_tasks)
        replace_scope.assert_called_once()
        self.assertGreaterEqual(record_change.call_count, 1)
        _, _, scoped_user = replace_scope.call_args.args
        self.assertIs(scoped_user, created)
        self.assertEqual(replace_scope.call_args.kwargs["view_all_tasks"], True)
        self.assertEqual(replace_scope.call_args.kwargs["visible_task_ids"], [3, 4])

    def test_update_viewer_user_applies_visible_task_scope(self) -> None:
        user = SimpleNamespace(
            id=2,
            workspace_id=7,
            role=UserRole.viewer,
            username="viewer001",
            display_name="viewer001",
            password_hash=hash_password("Viewer123456"),
            token_version=1,
            enabled=True,
            birthday=None,
            hire_date=None,
            view_all_tasks=False,
        )
        db = Mock()
        db.scalar.side_effect = [user]
        admin = SimpleNamespace(id=1, workspace_id=7)

        with (
            patch("app.services.admin_service._replace_viewer_visible_tasks") as replace_scope,
            patch("app.services.admin_service.record_workspace_change") as record_change,
        ):
            update_workspace_user(
                db,
                admin,  # type: ignore[arg-type]
                2,
                username=None,
                password=None,
                display_name=None,
                email=None,
                birthday=None,
                hire_date=None,
                view_all_tasks=False,
                visible_task_ids=[8],
                enabled=None,
                fields_set={"visible_task_ids"},
            )

        replace_scope.assert_called_once()
        self.assertGreaterEqual(record_change.call_count, 1)
        self.assertEqual(replace_scope.call_args.kwargs["visible_task_ids"], [8])

    def test_update_my_profile_records_profile_change(self) -> None:
        db = Mock()
        user = SimpleNamespace(
            id=2,
            workspace_id=7,
            role=UserRole.admin,
            display_name="旧名字",
            avatar=None,
            birthday=None,
            hire_date=None,
        )

        with patch("app.services.auth_service.record_workspace_change") as record_change:
            updated = update_my_profile(
                db,
                user,  # type: ignore[arg-type]
                display_name="新名字",
                avatar="https://example.com/avatar.png",
                birthday=None,
                hire_date=None,
                fields_set={"display_name", "avatar"},
            )

        self.assertIs(updated, user)
        self.assertEqual(user.display_name, "新名字")
        self.assertEqual(user.avatar, "https://example.com/avatar.png")
        record_change.assert_called_once()
        self.assertEqual(record_change.call_args.kwargs["stream"], "profile")

    def test_list_workspace_users_includes_viewer_visible_task_ids(self) -> None:
        admin_user = SimpleNamespace(
            id=1,
            workspace_id=7,
            username="admin@example.com",
            role=UserRole.admin,
            display_name="Admin",
            email="admin@example.com",
            avatar=None,
            birthday=None,
            hire_date=None,
            view_all_tasks=False,
            enabled=True,
            token_version=1,
            deleted_at=None,
            created_at=None,
        )
        viewer_user = SimpleNamespace(
            id=2,
            workspace_id=7,
            username="林见路",
            role=UserRole.viewer,
            display_name="林见路",
            email=None,
            avatar=None,
            birthday=None,
            hire_date=None,
            view_all_tasks=False,
            enabled=True,
            token_version=1,
            deleted_at=None,
            created_at=None,
        )
        view_all_user = SimpleNamespace(
            id=3,
            workspace_id=7,
            username="全部可见",
            role=UserRole.viewer,
            display_name="全部可见",
            email=None,
            avatar=None,
            birthday=None,
            hire_date=None,
            view_all_tasks=True,
            enabled=True,
            token_version=1,
            deleted_at=None,
            created_at=None,
        )
        db = Mock()
        db.scalars.return_value = [admin_user, viewer_user, view_all_user]
        db.execute.return_value = [(2, 8), (2, 9), (3, 10)]

        users = list_workspace_users(db, admin_user)  # type: ignore[arg-type]

        by_id = {item["id"]: item for item in users}
        self.assertEqual(by_id[2]["visible_task_ids"], [8, 9])
        self.assertEqual(by_id[3]["view_all_tasks"], True)
        self.assertEqual(by_id[3]["visible_task_ids"], [])

    def test_workspace_user_public_payload_includes_saved_viewer_scope(self) -> None:
        viewer_user = SimpleNamespace(
            id=2,
            workspace_id=7,
            username="林见路",
            role=UserRole.viewer,
            display_name="林见路",
            email=None,
            avatar=None,
            birthday=None,
            hire_date=None,
            view_all_tasks=False,
            enabled=True,
            token_version=1,
            deleted_at=None,
            created_at=None,
        )
        db = Mock()
        db.scalars.return_value = [8, 9]
        admin = SimpleNamespace(workspace_id=7)

        payload = workspace_user_public_payload(db, admin, viewer_user)  # type: ignore[arg-type]

        self.assertEqual(payload["visible_task_ids"], [8, 9])

    def test_login_uses_first_match_when_legacy_duplicates_exist(self) -> None:
        password_hash = hash_password("SamePass123")
        first_user = SimpleNamespace(
            id=11,
            workspace_id=1,
            username="张三",
            password_hash=password_hash,
            role=UserRole.operator,
            token_version=1,
            enabled=True,
            last_login_at=None,
        )
        second_user = SimpleNamespace(
            id=22,
            workspace_id=2,
            username="张三",
            password_hash=password_hash,
            role=UserRole.operator,
            token_version=1,
            enabled=True,
            last_login_at=None,
        )
        db = Mock()
        db.scalars.return_value = [first_user, second_user]

        with patch("app.services.auth_service.issue_token_pair", return_value=("access-token", "refresh-token")):
            user, access_token, refresh_token = login_user(
                db,  # type: ignore[arg-type]
                username="张三",
                password="SamePass123",
            )

        self.assertIs(user, first_user)
        self.assertEqual(access_token, "access-token")
        self.assertEqual(refresh_token, "refresh-token")


if __name__ == "__main__":
    unittest.main()
