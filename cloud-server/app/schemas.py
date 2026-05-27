from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class APIMessage(BaseModel):
    message: str


class CloudOpsReportResponse(BaseModel):
    report: dict[str, Any]
    text: str


class CloudMaintenanceRequest(BaseModel):
    dry_run: bool = True


class CloudMaintenanceResponse(BaseModel):
    dry_run: bool
    expired_upload_sessions: dict[str, int]
    dead_letters: dict[str, int]
    change_log: dict[str, int]
    orphan_files: dict[str, int]


class UserPublic(BaseModel):
    id: int
    workspace_id: int
    username: str
    role: Literal["admin", "operator", "viewer"]
    display_name: str | None = None
    email: str | None = None
    avatar: str | None = None
    birthday: date | None = None
    hire_date: date | None = None
    view_all_tasks: bool = False
    visible_task_ids: list[int] = Field(default_factory=list)
    enabled: bool
    token_version: int
    deleted_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    workspace_name: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=128)


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)
    device_id: str | None = Field(default=None, max_length=256)
    app_version: str | None = Field(default=None, max_length=32)


class ResendEmailVerificationRequest(BaseModel):
    email: EmailStr


class RequestPasswordResetRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=128)
    device_id: str | None = Field(default=None, max_length=256)
    app_version: str | None = Field(default=None, max_length=32)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserPublic


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["operator", "viewer"]
    display_name: str | None = Field(default=None, max_length=128)
    email: EmailStr | None = None
    birthday: date | None = None
    hire_date: date | None = None
    view_all_tasks: bool = False
    visible_task_ids: list[int] = Field(default_factory=list, max_length=1000)


class UpdateUserRequest(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    email: EmailStr | None = None
    birthday: date | None = None
    hire_date: date | None = None
    view_all_tasks: bool | None = None
    visible_task_ids: list[int] | None = Field(default=None, max_length=1000)
    enabled: bool | None = None


class UpdateMyProfileRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    avatar: str | None = Field(default=None, max_length=1_000_000)
    birthday: date | None = None
    hire_date: date | None = None


class BrandTaskPublic(BaseModel):
    id: int
    workspace_id: int
    task_key: str
    name: str
    brand: str
    config_json: dict[str, Any]
    config_version: int
    enabled: bool
    deleted_at: datetime | None = None
    delete_expires_at: datetime | None = None
    created_at: datetime
    assigned_operator_user_id: int | None = None
    assigned_operator_username: str | None = None
    assigned_operator_display_name: str | None = None
    assigned_viewer_user_ids: list[int] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class VisibleBrandTaskPublic(BrandTaskPublic):
    access_level: Literal["admin", "operate", "view"]


class CreateTaskRequest(BaseModel):
    task_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    brand: str = Field(min_length=1, max_length=128)
    config_json: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class UpdateTaskRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    brand: str | None = Field(default=None, min_length=1, max_length=128)
    config_json: dict[str, Any] | None = None
    enabled: bool | None = None
    expected_config_version: int | None = Field(default=None, ge=1)


class AssignTaskRequest(BaseModel):
    user_id: int
    access_level: Literal["operate", "view"]
    note: str | None = Field(default=None, max_length=1000)


class SyncEventIn(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=96)
    payload: dict[str, Any] = Field(default_factory=dict)


class SyncEventsRequest(BaseModel):
    events: list[SyncEventIn] = Field(default_factory=list, max_length=500)


class SyncEventsResponse(BaseModel):
    accepted: int
    duplicates: int


class SyncChangesRequest(BaseModel):
    known_snapshot: dict[str, Any] = Field(default_factory=dict)
    task_cursors: dict[int, int] = Field(default_factory=dict, max_length=200)
    task_day_status_cursors: dict[int, int] = Field(default_factory=dict, max_length=200)


class SyncChangesResponse(BaseModel):
    snapshot: dict[str, Any]
    event_id: str
    events: list[str]
    full_task_pull_required: bool
    changed_task_ids: list[int] = Field(default_factory=list)
    run_record_task_ids: list[int]
    run_record_max_ids: dict[int, int]
    task_day_status_task_ids: list[int] = Field(default_factory=list)
    task_day_status_max_ids: dict[int, int] = Field(default_factory=dict)
    reference_changed: bool


class CloudCapabilityResponse(BaseModel):
    capabilities: list[str]
    limits: dict[str, int]
    ttl_seconds: dict[str, int]


class SyncBatchEventIn(SyncEventIn):
    stream: str | None = Field(default=None, max_length=32)
    partition_key: str | None = Field(default=None, max_length=256)
    seq: int | None = Field(default=None, ge=0)


class SyncBatchRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    events: list[SyncBatchEventIn] = Field(default_factory=list, max_length=500)


class SyncBatchResponse(BaseModel):
    batch_id: str
    accepted: int
    duplicates: int
    rejected: int
    pending_materialization: int
    retry_after_seconds: int = 0
    queue_depth_hint: int = 0


class StateDeltaRequest(BaseModel):
    device_id: str | None = Field(default=None, max_length=256)
    cursors: dict[str, int] = Field(default_factory=dict, max_length=32)
    limit: int = Field(default=500, ge=1, le=1000)
    priority: list[str] = Field(default_factory=list, max_length=16)
    reset_token: str | None = Field(default=None, max_length=256)
    bootstrap_cursor: str | None = Field(default=None, max_length=256)


class StateDeltaResponse(BaseModel):
    changes: list[dict[str, Any]] = Field(default_factory=list)
    next_cursors: dict[str, int] = Field(default_factory=dict)
    has_more: bool = False
    object_refs: list[dict[str, Any]] = Field(default_factory=list)
    reset_required: bool = False
    reset_token: str | None = None
    bootstrap_cursor: str | None = None
    retry_after_seconds: int = 0


class ObjectUploadCreateRequest(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(gt=0)
    content_type: str = Field(min_length=1, max_length=128)
    storage_size_bytes: int | None = Field(default=None, gt=0)
    compression: str | None = Field(default="auto", max_length=32)


class ObjectUploadResponse(BaseModel):
    strategy: Literal["inline", "already_exists", "single_put", "multipart"]
    object_id: str | None = None
    session_id: str | None = None
    sha256: str
    size_bytes: int
    storage_size_bytes: int
    content_type: str
    compression: str
    storage_key: str
    upload: dict[str, Any] | None = None
    part_size_bytes: int | None = None
    parts_total: int | None = None
    expires_at: datetime | None = None


class ObjectPartsPresignRequest(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=100)


class ObjectPartsPresignResponse(BaseModel):
    session_id: str
    part_size_bytes: int
    parts_total: int
    upload_urls: list[dict[str, Any]]
    expires_at: datetime


class ObjectPartCompleteRequest(BaseModel):
    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class ObjectPartCompleteResponse(BaseModel):
    session_id: str
    part_number: int
    parts_completed: int
    parts_total: int


class ObjectUploadCompleteRequest(BaseModel):
    storage_size_bytes: int | None = Field(default=None, gt=0)
    compression: str | None = Field(default="auto", max_length=32)


class ObjectUploadCompleteResponse(BaseModel):
    object_id: str
    status: str
    storage_key: str
    sha256: str
    size_bytes: int
    storage_size_bytes: int
    content_type: str
    compression: str


class ObjectDownloadResponse(BaseModel):
    object_id: str
    download_url: str
    expires_at: datetime
    content_type: str
    size_bytes: int
    storage_size_bytes: int
    compression: str


class AgentCommandCreateRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    payload_json: dict[str, Any] = Field(default_factory=dict)
    target_device_id: str | None = Field(default=None, max_length=256)
    target_role: str | None = Field(default=None, max_length=64)


class AgentCommandPublic(BaseModel):
    id: str
    workspace_id: int
    target_device_id: str | None = None
    target_role: str | None = None
    status: str
    visibility_until: datetime | None = None
    idempotency_key: str
    payload_json: dict[str, Any]
    cancel_requested_at: datetime | None = None
    expires_at: datetime
    created_at: datetime


class AgentCommandClaimRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)
    target_role: str | None = Field(default=None, max_length=64)
    last_seen_command_id: str | None = Field(default=None, max_length=36)


class AgentCommandClaimResponse(BaseModel):
    command: AgentCommandPublic | None = None


class AgentHeartbeatRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)


class AgentResultChunkRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)
    seq: int = Field(ge=0)
    payload_json: dict[str, Any] = Field(default_factory=dict)
    is_final: bool = False
    final_status: Literal["completed", "failed", "cancelled"] | None = None


class AgentResultChunkAck(BaseModel):
    command_id: str
    seq: int
    is_final: bool
    status: str


class AgentResultChunkPublic(BaseModel):
    command_id: str
    seq: int
    payload_json: dict[str, Any]
    is_final: bool
    created_at: str


class RunRecordPublic(BaseModel):
    id: int
    workspace_id: int
    task_id: int
    executed_by: int
    platform: str
    keyword: str
    brand: str
    mode: str
    result_json: dict[str, Any]
    idempotency_key: str
    executed_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class RunRecordsBatchRequest(BaseModel):
    task_cursors: dict[int, int] = Field(default_factory=dict, max_length=200)
    limit_per_task: int = Field(default=6000, ge=1, le=10000)


class RunRecordsBatchResponse(BaseModel):
    records: dict[int, list[RunRecordPublic]]


class TaskDayStatusEventsBatchRequest(BaseModel):
    task_cursors: dict[int, int] = Field(default_factory=dict, max_length=200)
    limit_per_task: int = Field(default=500, ge=1, le=2000)


class TaskDayStatusEventsBatchResponse(BaseModel):
    events: dict[int, list[dict[str, Any]]]


class ArticleTaskLinkPublic(BaseModel):
    task_id: int
    source: str
    confidence: int
    reason_json: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ArticlePublic(BaseModel):
    id: int
    workspace_id: int
    canonical_url: str
    url_hash: str
    title: str | None = None
    source: str | None = None
    media_type: str | None = None
    published_at: datetime | None = None
    payload_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    task_links: list[ArticleTaskLinkPublic] = Field(default_factory=list)


class ArticlesResponse(BaseModel):
    articles: list[ArticlePublic]
    count: int
    max_updated_at: str = ""
    max_article_id: int = 0


class ArticleClassificationJobPublic(BaseModel):
    id: int
    workspace_id: int
    article_id: int
    status: Literal["unresolved", "resolved", "ignored"]
    reason_json: dict[str, Any]
    resolved_task_id: int | None = None
    resolved_by: int | None = None
    created_at: datetime
    updated_at: datetime
    article: ArticlePublic


class ResolveArticleClassificationRequest(BaseModel):
    task_id: int
    reason: str | None = Field(default=None, max_length=1000)


class IgnoreArticleClassificationRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class ReferenceRankingItem(BaseModel):
    url: str
    score: float
    platforms: list[str]
    days: int
    events: int


class ReferenceRankingResponse(BaseModel):
    algorithm_version: str
    items: list[ReferenceRankingItem]


class UpdateManifestPackage(BaseModel):
    url: str
    sha256: str
    signature: str | None = None


class UpdateManifestChannel(BaseModel):
    version: str
    channel: str
    published_at: str | None = None
    notes: str | None = None
    packages: dict[str, UpdateManifestPackage]


class UpdateManifestResponse(BaseModel):
    app_name: str = "Surfaced"
    channels: dict[str, UpdateManifestChannel]
