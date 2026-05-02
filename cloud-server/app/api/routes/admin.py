from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AdminUser, DbSession
from app.schemas import (
    APIMessage,
    AssignTaskRequest,
    BrandTaskPublic,
    CreateTaskRequest,
    CreateUserRequest,
    UpdateUserRequest,
    UserPublic,
)
from app.services.admin_service import (
    assign_task_member,
    create_task,
    create_workspace_user,
    delete_workspace_user,
    list_tasks,
    list_workspace_users,
    update_workspace_user,
)

router = APIRouter()


@router.get("/users", response_model=list[UserPublic])
def users(admin: AdminUser, db: DbSession) -> list[UserPublic]:
    return list_workspace_users(db, admin)


@router.post("/users", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def create_user(payload: CreateUserRequest, admin: AdminUser, db: DbSession) -> UserPublic:
    return create_workspace_user(
        db,
        admin,
        username=payload.username,
        password=payload.password,
        role=payload.role,
        display_name=payload.display_name,
        email=str(payload.email) if payload.email else None,
    )


@router.patch("/users/{user_id}", response_model=UserPublic)
def update_user(user_id: int, payload: UpdateUserRequest, admin: AdminUser, db: DbSession) -> UserPublic:
    return update_workspace_user(
        db,
        admin,
        user_id,
        password=payload.password,
        display_name=payload.display_name,
        email=str(payload.email) if payload.email else None,
        enabled=payload.enabled,
    )


@router.delete("/users/{user_id}", response_model=APIMessage)
def delete_user(user_id: int, admin: AdminUser, db: DbSession) -> APIMessage:
    delete_workspace_user(db, admin, user_id)
    return APIMessage(message="ok")


@router.get("/tasks", response_model=list[BrandTaskPublic])
def tasks(admin: AdminUser, db: DbSession) -> list[BrandTaskPublic]:
    return list_tasks(db, admin)


@router.post("/tasks", response_model=BrandTaskPublic, status_code=status.HTTP_201_CREATED)
def create_brand_task(payload: CreateTaskRequest, admin: AdminUser, db: DbSession) -> BrandTaskPublic:
    return create_task(
        db,
        admin,
        task_key=payload.task_key,
        name=payload.name,
        brand=payload.brand,
        config_json=payload.config_json,
        enabled=payload.enabled,
    )


@router.post("/tasks/{task_id}/members", response_model=APIMessage)
def set_task_member(task_id: int, payload: AssignTaskRequest, admin: AdminUser, db: DbSession) -> APIMessage:
    assign_task_member(
        db,
        admin,
        task_id=task_id,
        user_id=payload.user_id,
        access_level=payload.access_level,
        note=payload.note,
    )
    return APIMessage(message="ok")

