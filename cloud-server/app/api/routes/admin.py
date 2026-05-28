from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import AdminUser, DbSession
from app.schemas import (
    APIMessage,
    AssignTaskRequest,
    ArticleClassificationJobPublic,
    BrandTaskPublic,
    CloudMaintenanceRequest,
    CloudMaintenanceResponse,
    CloudOpsReportResponse,
    CloudShadowReconcileResponse,
    CloudSyncQueueRequeueRequest,
    CloudSyncQueueRequeueResponse,
    CreateTaskRequest,
    CreateUserRequest,
    IgnoreArticleClassificationRequest,
    ResolveArticleClassificationRequest,
    UpdateTaskRequest,
    UpdateUserRequest,
    UserPublic,
)
from app.services.article_classification_service import (
    ignore_article_classification_job,
    list_article_classification_jobs,
    resolve_article_classification_job,
)
from app.services.admin_service import (
    assign_task_member,
    clear_task_operator_assignment,
    create_task,
    create_workspace_user,
    delete_workspace_user,
    delete_task,
    list_tasks,
    list_deleted_tasks,
    list_workspace_users,
    restore_task,
    update_task,
    update_workspace_user,
    workspace_user_public_payload,
)
from app.services.cloud_maintenance_service import run_cloud_maintenance
from app.services.object_storage_diagnostics import build_object_storage_report, format_object_storage_report
from app.services.shadow_reconcile_service import build_shadow_reconcile_report, format_shadow_reconcile_report
from app.services.sync_queue_diagnostics import build_sync_queue_report, format_sync_queue_report
from app.services.sync_queue_ops_service import requeue_sync_queue_items

router = APIRouter()


@router.get("/users", response_model=list[UserPublic])
def users(admin: AdminUser, db: DbSession) -> list[dict]:
    return list_workspace_users(db, admin)


@router.get("/ops/sync-queue", response_model=CloudOpsReportResponse)
def sync_queue_doctor(admin: AdminUser, db: DbSession) -> CloudOpsReportResponse:
    del admin
    report = build_sync_queue_report(db)
    return CloudOpsReportResponse(report=report, text=format_sync_queue_report(report))


@router.post("/ops/sync-queue/requeue", response_model=CloudSyncQueueRequeueResponse)
def sync_queue_requeue(
    payload: CloudSyncQueueRequeueRequest,
    admin: AdminUser,
    db: DbSession,
) -> CloudSyncQueueRequeueResponse:
    del admin
    return CloudSyncQueueRequeueResponse(
        **requeue_sync_queue_items(
            db,
            workspace_id=payload.workspace_id,
            statuses=list(payload.statuses),
            partition_key=payload.partition_key or "",
            limit=payload.limit,
            dry_run=payload.dry_run,
        )
    )


@router.get("/ops/object-storage", response_model=CloudOpsReportResponse)
def object_storage_doctor(admin: AdminUser, db: DbSession) -> CloudOpsReportResponse:
    del admin
    report = build_object_storage_report(db)
    return CloudOpsReportResponse(report=report, text=format_object_storage_report(report))


@router.get("/ops/shadow-reconcile", response_model=CloudShadowReconcileResponse)
def shadow_reconcile_doctor(admin: AdminUser, db: DbSession) -> CloudShadowReconcileResponse:
    del admin
    report = build_shadow_reconcile_report(db)
    return CloudShadowReconcileResponse(report=report, text=format_shadow_reconcile_report(report))


@router.post("/ops/maintenance", response_model=CloudMaintenanceResponse)
def cloud_maintenance(payload: CloudMaintenanceRequest, admin: AdminUser, db: DbSession) -> CloudMaintenanceResponse:
    del admin
    return CloudMaintenanceResponse(**run_cloud_maintenance(db, dry_run=payload.dry_run))


@router.post("/users", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def create_user(payload: CreateUserRequest, admin: AdminUser, db: DbSession) -> dict:
    user = create_workspace_user(
        db,
        admin,
        username=payload.username,
        password=payload.password,
        role=payload.role,
        display_name=payload.display_name,
        email=str(payload.email) if payload.email else None,
        birthday=payload.birthday,
        hire_date=payload.hire_date,
        view_all_tasks=payload.view_all_tasks,
        visible_task_ids=payload.visible_task_ids,
    )
    return workspace_user_public_payload(db, admin, user)


@router.patch("/users/{user_id}", response_model=UserPublic)
def update_user(user_id: int, payload: UpdateUserRequest, admin: AdminUser, db: DbSession) -> dict:
    user = update_workspace_user(
        db,
        admin,
        user_id,
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
        email=(
            str(payload.email)
            if payload.email
            else ("" if "email" in payload.model_fields_set else None)
        ),
        birthday=payload.birthday,
        hire_date=payload.hire_date,
        view_all_tasks=payload.view_all_tasks,
        visible_task_ids=payload.visible_task_ids,
        enabled=payload.enabled,
        fields_set=set(payload.model_fields_set),
    )
    return workspace_user_public_payload(db, admin, user)


@router.delete("/users/{user_id}", response_model=APIMessage)
def delete_user(user_id: int, admin: AdminUser, db: DbSession) -> APIMessage:
    delete_workspace_user(db, admin, user_id)
    return APIMessage(message="ok")


@router.get("/articles/classification-jobs", response_model=list[ArticleClassificationJobPublic])
def article_classification_jobs(
    admin: AdminUser,
    db: DbSession,
    status_filter: str = Query(default="unresolved", alias="status", max_length=32),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    return list_article_classification_jobs(db, admin, status_filter=status_filter, limit=limit)


@router.post("/articles/classification-jobs/{job_id}/resolve", response_model=ArticleClassificationJobPublic)
def resolve_article_classification(
    job_id: int,
    payload: ResolveArticleClassificationRequest,
    admin: AdminUser,
    db: DbSession,
) -> dict:
    return resolve_article_classification_job(
        db,
        admin,
        job_id=job_id,
        task_id=payload.task_id,
        reason=payload.reason or "",
    )


@router.post("/articles/classification-jobs/{job_id}/ignore", response_model=ArticleClassificationJobPublic)
def ignore_article_classification(
    job_id: int,
    payload: IgnoreArticleClassificationRequest,
    admin: AdminUser,
    db: DbSession,
) -> dict:
    return ignore_article_classification_job(db, admin, job_id=job_id, reason=payload.reason or "")


@router.get("/tasks", response_model=list[BrandTaskPublic])
def tasks(admin: AdminUser, db: DbSession) -> list[BrandTaskPublic]:
    return list_tasks(db, admin)


@router.get("/tasks/deleted", response_model=list[BrandTaskPublic])
def deleted_tasks(admin: AdminUser, db: DbSession) -> list[BrandTaskPublic]:
    return list_deleted_tasks(db, admin)


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


@router.patch("/tasks/{task_id}", response_model=BrandTaskPublic)
def update_brand_task(task_id: int, payload: UpdateTaskRequest, admin: AdminUser, db: DbSession) -> BrandTaskPublic:
    return update_task(
        db,
        admin,
        task_id,
        name=payload.name,
        brand=payload.brand,
        config_json=payload.config_json,
        enabled=payload.enabled,
        expected_config_version=payload.expected_config_version,
    )


@router.delete("/tasks/{task_id}", response_model=BrandTaskPublic)
def delete_brand_task(task_id: int, admin: AdminUser, db: DbSession) -> BrandTaskPublic:
    return delete_task(db, admin, task_id)


@router.post("/tasks/{task_id}/restore", response_model=BrandTaskPublic)
def restore_brand_task(task_id: int, admin: AdminUser, db: DbSession) -> BrandTaskPublic:
    return restore_task(db, admin, task_id)


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


@router.delete("/tasks/{task_id}/members/operator", response_model=APIMessage)
def clear_task_operator(task_id: int, admin: AdminUser, db: DbSession) -> APIMessage:
    clear_task_operator_assignment(
        db,
        admin,
        task_id=task_id,
        note="清空品牌运营归属",
    )
    return APIMessage(message="ok")
