from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
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


def list_workspace_users(db: Session, admin: User) -> list[User]:
    return list(db.scalars(select(User).where(User.workspace_id == admin.workspace_id).order_by(User.id)))


def create_workspace_user(
    db: Session,
    admin: User,
    *,
    username: str,
    password: str,
    role: str,
    display_name: str | None,
    email: str | None,
) -> User:
    if role not in {UserRole.operator.value, UserRole.viewer.value}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
    user = User(
        workspace_id=admin.workspace_id,
        username=username.strip().lower(),
        password_hash=hash_password(password),
        role=UserRole(role),
        display_name=display_name,
        email=str(email).strip().lower() if email else None,
        email_verified=bool(email),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists") from exc
    db.refresh(user)
    return user


def update_workspace_user(
    db: Session,
    admin: User,
    user_id: int,
    *,
    password: str | None,
    display_name: str | None,
    email: str | None,
    enabled: bool | None,
) -> User:
    user = db.scalar(select(User).where(User.id == user_id, User.workspace_id == admin.workspace_id))
    if not user or user.role == UserRole.admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if password:
        user.password_hash = hash_password(password)
        user.token_version += 1
    if display_name is not None:
        user.display_name = display_name
    if email is not None:
        user.email = str(email).strip().lower() if email else None
        user.email_verified = bool(email)
    if enabled is not None and user.enabled != enabled:
        user.enabled = enabled
        user.token_version += 1
    user.updated_at = utc_now()
    db.commit()
    db.refresh(user)
    return user


def delete_workspace_user(db: Session, admin: User, user_id: int) -> None:
    user = db.scalar(select(User).where(User.id == user_id, User.workspace_id == admin.workspace_id))
    if not user or user.role == UserRole.admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    db.delete(user)
    db.commit()


def list_tasks(db: Session, admin: User) -> list[BrandTask]:
    return list(db.scalars(select(BrandTask).where(BrandTask.workspace_id == admin.workspace_id).order_by(BrandTask.id)))


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
    task = BrandTask(
        workspace_id=admin.workspace_id,
        task_key=task_key.strip(),
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


def assign_task_member(
    db: Session,
    admin: User,
    *,
    task_id: int,
    user_id: int,
    access_level: str,
    note: str | None,
) -> TaskMember:
    task = db.scalar(select(BrandTask).where(BrandTask.id == task_id, BrandTask.workspace_id == admin.workspace_id))
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    target = db.scalar(select(User).where(User.id == user_id, User.workspace_id == admin.workspace_id))
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

