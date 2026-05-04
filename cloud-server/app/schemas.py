from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class APIMessage(BaseModel):
    message: str


class UserPublic(BaseModel):
    id: int
    workspace_id: int
    username: str
    role: Literal["admin", "operator", "viewer"]
    display_name: str | None = None
    email: str | None = None
    enabled: bool
    token_version: int
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


class UpdateUserRequest(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    email: EmailStr | None = None
    enabled: bool | None = None


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
