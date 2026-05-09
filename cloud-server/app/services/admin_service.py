from __future__ import annotations

import secrets
from datetime import date, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password, utc_now
from app.models import (
    BrandTask,
    TaskAccessLevel,
    TaskAssignmentEvent,
    TaskMember,
    User,
    UserRole,
)


def list_workspace_users(db: Session, admin: User) -> list[dict]:
    users = list(
        db.scalars(
            select(User)
            .where(User.workspace_id == admin.workspace_id, User.deleted_at.is_(None))
            .order_by(User.id)
        )
    )
    viewer_ids = [int(user.id) for user in users if user.role == UserRole.viewer]
    visible_task_ids_by_user: dict[int, list[int]] = {}
    if viewer_ids:
        rows = db.execute(
            select(TaskMember.user_id, TaskMember.task_id)
            .where(
                TaskMember.workspace_id == admin.workspace_id,
                TaskMember.user_id.in_(viewer_ids),
                TaskMember.access_level == TaskAccessLevel.view,
            )
            .order_by(TaskMember.user_id, TaskMember.task_id)
        )
        for user_id, task_id in rows:
            visible_task_ids_by_user.setdefault(int(user_id), []).append(int(task_id))
    return [
        _user_to_public_payload(user, visible_task_ids_by_user.get(int(user.id), []))
        for user in users
    ]


def _user_to_public_payload(user: User, visible_task_ids: list[int] | None = None) -> dict:
    role = user.role.value if isinstance(user.role, UserRole) else user.role
    return {
        "id": user.id,
        "workspace_id": user.workspace_id,
        "username": user.username,
        "role": role,
        "display_name": user.display_name,
        "email": user.email,
        "avatar": user.avatar,
        "birthday": user.birthday,
        "hire_date": user.hire_date,
        "view_all_tasks": bool(getattr(user, "view_all_tasks", False)),
        "visible_task_ids": [] if bool(getattr(user, "view_all_tasks", False)) else list(visible_task_ids or []),
        "enabled": user.enabled,
        "token_version": user.token_version,
        "deleted_at": user.deleted_at,
        "created_at": user.created_at,
    }


def workspace_user_public_payload(db: Session, admin: User, user: User) -> dict:
    visible_task_ids: list[int] = []
    role = user.role.value if isinstance(user.role, UserRole) else user.role
    if (
        role == UserRole.viewer.value
        and not bool(getattr(user, "view_all_tasks", False))
    ):
        visible_task_ids = _viewer_visible_task_ids(db, admin.workspace_id, int(user.id))
    return _user_to_public_payload(user, visible_task_ids)


def create_workspace_user(
    db: Session,
    admin: User,
    *,
    username: str,
    password: str,
    role: str,
    display_name: str | None,
    email: str | None,
    birthday: date | None,
    hire_date: date | None,
    view_all_tasks: bool = False,
    visible_task_ids: list[int] | None = None,
) -> User:
    if role not in {UserRole.operator.value, UserRole.viewer.value}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
    normalized_username = _normalize_username(username)
    if not normalized_username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username is required")
    if _active_username_owner_id(db, normalized_username) is not None:
        _raise_username_conflict(db, normalized_username)
    user = User(
        workspace_id=admin.workspace_id,
        username=normalized_username,
        password_hash=hash_password(password),
        role=UserRole(role),
        display_name=normalized_username,
        email=str(email).strip().lower() if email else None,
        email_verified=bool(email),
        birthday=birthday,
        hire_date=hire_date,
        view_all_tasks=bool(view_all_tasks) if role == UserRole.viewer.value else False,
    )
    db.add(user)
    try:
        db.flush()
        if user.role == UserRole.viewer and (view_all_tasks or visible_task_ids is not None):
            _replace_viewer_visible_tasks(
                db,
                admin,
                user,
                view_all_tasks=bool(view_all_tasks),
                visible_task_ids=visible_task_ids or [],
            )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        _raise_username_conflict(db, normalized_username)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists") from exc
    db.refresh(user)
    return user


def update_workspace_user(
    db: Session,
    admin: User,
    user_id: int,
    *,
    username: str | None,
    password: str | None,
    display_name: str | None,
    email: str | None,
    birthday: date | None,
    hire_date: date | None,
    view_all_tasks: bool | None = None,
    visible_task_ids: list[int] | None = None,
    enabled: bool | None = None,
    fields_set: set[str] | None = None,
) -> User:
    fields_set = fields_set or set()
    user = db.scalar(
        select(User).where(
            User.id == user_id,
            User.workspace_id == admin.workspace_id,
            User.deleted_at.is_(None),
        )
    )
    if not user or user.role == UserRole.admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    next_username = None
    if username is not None:
        next_username = _normalize_username(username)
    elif "display_name" in fields_set and display_name is not None:
        next_username = _normalize_username(display_name)
    if next_username is not None:
        if not next_username:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username is required")
        if _active_username_owner_id(db, next_username, exclude_user_id=user.id) is not None:
            _raise_username_conflict(db, next_username, exclude_user_id=user.id)
        user.username = next_username
        user.display_name = next_username
    if password:
        user.password_hash = hash_password(password)
        user.token_version += 1
    if email is not None:
        user.email = str(email).strip().lower() if email else None
        user.email_verified = bool(email)
    if "birthday" in fields_set:
        user.birthday = birthday
    if "hire_date" in fields_set:
        user.hire_date = hire_date
    if user.role == UserRole.viewer and (
        "view_all_tasks" in fields_set or "visible_task_ids" in fields_set
    ):
        _replace_viewer_visible_tasks(
            db,
            admin,
            user,
            view_all_tasks=bool(view_all_tasks),
            visible_task_ids=visible_task_ids or [],
        )
    if enabled is not None and user.enabled != enabled:
        user.enabled = enabled
        user.token_version += 1
    user.updated_at = utc_now()
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if next_username is not None:
            _raise_username_conflict(db, next_username, exclude_user_id=user.id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists") from exc
    db.refresh(user)
    return user


def _replace_viewer_visible_tasks(
    db: Session,
    admin: User,
    user: User,
    *,
    view_all_tasks: bool,
    visible_task_ids: list[int],
) -> None:
    if user.role != UserRole.viewer:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only viewer accounts support visible tasks")

    current_members = list(
        db.scalars(
            select(TaskMember).where(
                TaskMember.workspace_id == admin.workspace_id,
                TaskMember.user_id == user.id,
                TaskMember.access_level == TaskAccessLevel.view,
            )
        )
    )
    current_by_task_id = {int(member.task_id): member for member in current_members}
    user.view_all_tasks = bool(view_all_tasks)

    if user.view_all_tasks:
        for member in current_members:
            db.delete(member)
        user.updated_at = utc_now()
        return

    desired_task_ids = set(_validate_visible_task_ids(db, admin.workspace_id, visible_task_ids))
    current_task_ids = set(current_by_task_id.keys())

    for task_id in sorted(current_task_ids - desired_task_ids):
        member = current_by_task_id[task_id]
        db.add(
            TaskAssignmentEvent(
                workspace_id=admin.workspace_id,
                task_id=task_id,
                from_user_id=user.id,
                to_user_id=None,
                access_level=TaskAccessLevel.view,
                note="调整浏览账号可见品牌",
                assigned_by=admin.id,
            )
        )
        db.delete(member)

    for task_id in sorted(desired_task_ids - current_task_ids):
        db.add(
            TaskMember(
                workspace_id=admin.workspace_id,
                task_id=task_id,
                user_id=user.id,
                access_level=TaskAccessLevel.view,
                assigned_by=admin.id,
            )
        )
        db.add(
            TaskAssignmentEvent(
                workspace_id=admin.workspace_id,
                task_id=task_id,
                from_user_id=None,
                to_user_id=user.id,
                access_level=TaskAccessLevel.view,
                note="调整浏览账号可见品牌",
                assigned_by=admin.id,
            )
        )
    user.updated_at = utc_now()


def _validate_visible_task_ids(db: Session, workspace_id: int, task_ids: list[int]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_task_id in task_ids or []:
        try:
            task_id = int(raw_task_id)
        except Exception:
            continue
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        normalized.append(task_id)
    if not normalized:
        return []

    found_ids = set(
        int(task_id)
        for task_id in db.scalars(
            select(BrandTask.id).where(
                BrandTask.workspace_id == workspace_id,
                BrandTask.id.in_(normalized),
                BrandTask.deleted_at.is_(None),
            )
        )
    )
    missing = [task_id for task_id in normalized if task_id not in found_ids]
    if missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Visible task not found")
    return normalized


def _viewer_visible_task_ids(db: Session, workspace_id: int, user_id: int) -> list[int]:
    return [
        int(task_id)
        for task_id in db.scalars(
            select(TaskMember.task_id)
            .where(
                TaskMember.workspace_id == workspace_id,
                TaskMember.user_id == user_id,
                TaskMember.access_level == TaskAccessLevel.view,
            )
            .order_by(TaskMember.task_id)
        )
    ]


def _normalize_username(username: str) -> str:
    return str(username or "").strip()


def _active_username_owner_id(db: Session, username: str, *, exclude_user_id: int | None = None) -> int | None:
    normalized = _normalize_username(username)
    if not normalized:
        return None
    query = select(User.id).where(
        func.lower(User.username) == normalized.lower(),
        User.deleted_at.is_(None),
    )
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    owner_id = db.scalar(query.limit(1))
    return int(owner_id) if owner_id is not None else None


def _available_username_suggestion(db: Session, username: str, *, exclude_user_id: int | None = None) -> str:
    base = _normalize_username(username) or "user"
    for counter in range(1, 1000):
        suffix = f"{counter:02d}" if counter < 100 else str(counter)
        candidate = f"{base[: max(1, 128 - len(suffix))]}{suffix}"
        if _active_username_owner_id(db, candidate, exclude_user_id=exclude_user_id) is None:
            return candidate
    suffix = secrets.token_hex(3)
    return f"{base[: max(1, 128 - len(suffix))]}{suffix}"


def _raise_username_conflict(db: Session, username: str, *, exclude_user_id: int | None = None) -> None:
    suggestion = _available_username_suggestion(db, username, exclude_user_id=exclude_user_id)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": f"账号名「{username}」已被使用，建议使用「{suggestion}」",
            "suggested_username": suggestion,
        },
    )


def delete_workspace_user(db: Session, admin: User, user_id: int) -> None:
    user = db.scalar(
        select(User).where(
            User.id == user_id,
            User.workspace_id == admin.workspace_id,
            User.deleted_at.is_(None),
        )
    )
    if not user or user.role == UserRole.admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    now = utc_now()
    memberships = list(
        db.scalars(
            select(TaskMember).where(
                TaskMember.workspace_id == admin.workspace_id,
                TaskMember.user_id == user.id,
            )
        )
    )
    for member in memberships:
        db.add(
            TaskAssignmentEvent(
                workspace_id=admin.workspace_id,
                task_id=member.task_id,
                from_user_id=user.id,
                to_user_id=None,
                access_level=member.access_level,
                note="删除普通账号，解除任务归属",
                assigned_by=admin.id,
            )
        )
        db.delete(member)
    user.enabled = False
    user.token_version += 1
    user.deleted_at = now
    user.updated_at = now
    db.commit()


def list_tasks(db: Session, admin: User) -> list[dict]:
    _purge_expired_deleted_tasks(db, admin.workspace_id)
    tasks = list(
        db.scalars(
            select(BrandTask)
            .where(BrandTask.workspace_id == admin.workspace_id, BrandTask.deleted_at.is_(None))
            .order_by(BrandTask.id)
        )
    )
    if not tasks:
        return []
    task_ids = [task.id for task in tasks]
    member_rows = db.execute(
        select(TaskMember, User)
        .join(User, User.id == TaskMember.user_id)
        .where(TaskMember.workspace_id == admin.workspace_id, TaskMember.task_id.in_(task_ids))
    ).all()
    members_by_task: dict[int, list[tuple[TaskMember, User]]] = {}
    for member, user in member_rows:
        members_by_task.setdefault(member.task_id, []).append((member, user))
    return [_task_to_admin_payload(task, members_by_task.get(task.id, [])) for task in tasks]


def list_deleted_tasks(db: Session, admin: User) -> list[dict]:
    _purge_expired_deleted_tasks(db, admin.workspace_id)
    tasks = list(
        db.scalars(
            select(BrandTask)
            .where(BrandTask.workspace_id == admin.workspace_id, BrandTask.deleted_at.is_not(None))
            .order_by(BrandTask.deleted_at.desc(), BrandTask.id.desc())
        )
    )
    if not tasks:
        return []
    return [_task_to_admin_payload(task, []) for task in tasks]


def _task_to_admin_payload(task: BrandTask, members: list[tuple[TaskMember, User]]) -> dict:
    operator_user: User | None = None
    viewer_ids: list[int] = []
    for member, user in members:
        if member.access_level == TaskAccessLevel.operate and operator_user is None:
            operator_user = user
        elif member.access_level == TaskAccessLevel.view:
            viewer_ids.append(user.id)
    return {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "task_key": task.task_key,
        "name": task.name,
        "brand": task.brand,
        "config_json": task.config_json or {},
        "config_version": task.config_version,
        "enabled": task.enabled,
        "deleted_at": task.deleted_at,
        "delete_expires_at": task.delete_expires_at,
        "created_at": task.created_at,
        "assigned_operator_user_id": operator_user.id if operator_user else None,
        "assigned_operator_username": operator_user.username if operator_user else None,
        "assigned_operator_display_name": operator_user.display_name if operator_user else None,
        "assigned_viewer_user_ids": viewer_ids,
    }


def create_task(
    db: Session,
    admin: User,
    *,
    task_key: str,
    name: str,
    brand: str,
    config_json: dict,
    enabled: bool,
) -> BrandTask:
    _purge_expired_deleted_tasks(db, admin.workspace_id)
    normalized_task_key = _resolve_create_task_key(db, admin.workspace_id, task_key.strip())
    task = BrandTask(
        workspace_id=admin.workspace_id,
        task_key=normalized_task_key,
        name=name.strip(),
        brand=brand.strip(),
        config_json=config_json or {},
        enabled=enabled,
    )
    db.add(task)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task key already exists") from exc
    db.refresh(task)
    return task


def _resolve_create_task_key(db: Session, workspace_id: int, requested_key: str) -> str:
    if not requested_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Task key is required")
    existing = db.scalar(
        select(BrandTask).where(
            BrandTask.workspace_id == workspace_id,
            BrandTask.task_key == requested_key,
        )
    )
    if not existing:
        return requested_key[:128]
    if existing.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task key already exists")

    for index in range(1, 1000):
        candidate = _new_task_key_variant(requested_key, index)
        collision = db.scalar(
            select(BrandTask.id).where(
                BrandTask.workspace_id == workspace_id,
                BrandTask.task_key == candidate,
            )
        )
        if not collision:
            return candidate
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task key already exists")


def _new_task_key_variant(task_key: str, index: int) -> str:
    suffix = "-new" if index <= 1 else f"-new-{index}"
    base_limit = max(1, 128 - len(suffix))
    base = task_key[:base_limit].rstrip("-_.") or "task"
    return f"{base}{suffix}"[:128]


def update_task(
    db: Session,
    admin: User,
    task_id: int,
    *,
    name: str | None,
    brand: str | None,
    config_json: dict | None,
    enabled: bool | None,
    expected_config_version: int | None,
) -> BrandTask:
    _purge_expired_deleted_tasks(db, admin.workspace_id)
    task = db.scalar(
        select(BrandTask).where(
            BrandTask.id == task_id,
            BrandTask.workspace_id == admin.workspace_id,
            BrandTask.deleted_at.is_(None),
        )
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if expected_config_version is not None and int(task.config_version or 1) != int(expected_config_version):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task config version conflict")

    changed = False
    if name is not None:
        normalized_name = name.strip()
        changed = changed or task.name != normalized_name
        task.name = normalized_name
    if brand is not None:
        normalized_brand = brand.strip()
        changed = changed or task.brand != normalized_brand
        task.brand = normalized_brand
    if config_json is not None:
        normalized_config = config_json or {}
        changed = changed or (task.config_json or {}) != normalized_config
        task.config_json = normalized_config
    if enabled is not None:
        normalized_enabled = bool(enabled)
        changed = changed or task.enabled != normalized_enabled
        task.enabled = normalized_enabled

    if changed:
        task.config_version = int(task.config_version or 1) + 1
        task.updated_at = utc_now()
        db.commit()
        db.refresh(task)
    return task


def assign_task_member(
    db: Session,
    admin: User,
    *,
    task_id: int,
    user_id: int,
    access_level: str,
    note: str | None,
) -> TaskMember:
    _purge_expired_deleted_tasks(db, admin.workspace_id)
    task = db.scalar(
        select(BrandTask).where(
            BrandTask.id == task_id,
            BrandTask.workspace_id == admin.workspace_id,
            BrandTask.deleted_at.is_(None),
        )
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    target = db.scalar(
        select(User).where(
            User.id == user_id,
            User.workspace_id == admin.workspace_id,
            User.deleted_at.is_(None),
        )
    )
    if not target or target.role == UserRole.admin or not target.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found")
    level = TaskAccessLevel(access_level)
    if target.role == UserRole.viewer and level == TaskAccessLevel.operate:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Viewer cannot receive operate access")

    existing = db.scalar(select(TaskMember).where(TaskMember.task_id == task.id, TaskMember.user_id == target.id))
    previous_user_id: int | None = None
    if existing:
        existing.access_level = level
        existing.assigned_by = admin.id
        existing.assigned_at = utc_now()
        member = existing
    else:
        member = TaskMember(
            workspace_id=admin.workspace_id,
            task_id=task.id,
            user_id=target.id,
            access_level=level,
            assigned_by=admin.id,
        )
        db.add(member)

    if level == TaskAccessLevel.operate:
        old_members = list(
            db.scalars(
                select(TaskMember).where(
                    TaskMember.task_id == task.id,
                    TaskMember.access_level == TaskAccessLevel.operate,
                    TaskMember.user_id != target.id,
                )
            )
        )
        for old_member in old_members:
            previous_user_id = old_member.user_id
            db.delete(old_member)

    event = TaskAssignmentEvent(
        workspace_id=admin.workspace_id,
        task_id=task.id,
        from_user_id=previous_user_id,
        to_user_id=target.id,
        access_level=level,
        note=note,
        assigned_by=admin.id,
    )
    db.add(event)
    db.commit()
    db.refresh(member)
    return member


def clear_task_operator_assignment(
    db: Session,
    admin: User,
    *,
    task_id: int,
    note: str | None = None,
) -> None:
    _purge_expired_deleted_tasks(db, admin.workspace_id)
    task = db.scalar(
        select(BrandTask).where(
            BrandTask.id == task_id,
            BrandTask.workspace_id == admin.workspace_id,
            BrandTask.deleted_at.is_(None),
        )
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    old_members = list(
        db.scalars(
            select(TaskMember).where(
                TaskMember.workspace_id == admin.workspace_id,
                TaskMember.task_id == task.id,
                TaskMember.access_level == TaskAccessLevel.operate,
            )
        )
    )
    if not old_members:
        return

    for old_member in old_members:
        db.add(
            TaskAssignmentEvent(
                workspace_id=admin.workspace_id,
                task_id=task.id,
                from_user_id=old_member.user_id,
                to_user_id=None,
                access_level=TaskAccessLevel.operate,
                note=note,
                assigned_by=admin.id,
            )
        )
        db.delete(old_member)
    db.commit()


def delete_task(db: Session, admin: User, task_id: int) -> BrandTask:
    _purge_expired_deleted_tasks(db, admin.workspace_id)
    task = db.scalar(
        select(BrandTask).where(
            BrandTask.id == task_id,
            BrandTask.workspace_id == admin.workspace_id,
            BrandTask.deleted_at.is_(None),
        )
    )
    if not task:
        deleted_task = db.scalar(
            select(BrandTask).where(
                BrandTask.id == task_id,
                BrandTask.workspace_id == admin.workspace_id,
                BrandTask.deleted_at.is_not(None),
            )
        )
        if deleted_task:
            return deleted_task
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    now = utc_now()
    task.deleted_at = now
    task.delete_expires_at = now + timedelta(days=3)
    task.deleted_by = admin.id
    task.config_version = int(task.config_version or 1) + 1
    task.updated_at = now
    db.commit()
    db.refresh(task)
    return task


def restore_task(db: Session, admin: User, task_id: int) -> BrandTask:
    _purge_expired_deleted_tasks(db, admin.workspace_id)
    task = db.scalar(
        select(BrandTask).where(
            BrandTask.id == task_id,
            BrandTask.workspace_id == admin.workspace_id,
            BrandTask.deleted_at.is_not(None),
        )
    )
    if not task:
        active_task = db.scalar(
            select(BrandTask).where(
                BrandTask.id == task_id,
                BrandTask.workspace_id == admin.workspace_id,
                BrandTask.deleted_at.is_(None),
            )
        )
        if active_task:
            return active_task
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    task.deleted_at = None
    task.delete_expires_at = None
    task.deleted_by = None
    task.config_version = int(task.config_version or 1) + 1
    task.updated_at = utc_now()
    db.commit()
    db.refresh(task)
    return task


def _purge_expired_deleted_tasks(db: Session, workspace_id: int) -> None:
    now = utc_now()
    expired_ids = list(
        db.scalars(
            select(BrandTask.id).where(
                BrandTask.workspace_id == workspace_id,
                BrandTask.deleted_at.is_not(None),
                BrandTask.delete_expires_at.is_not(None),
                BrandTask.delete_expires_at <= now,
            )
        )
    )
    if not expired_ids:
        return
    db.execute(delete(BrandTask).where(BrandTask.id.in_(expired_ids)))
    db.commit()
