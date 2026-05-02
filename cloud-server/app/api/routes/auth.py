from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas import AdminRegisterRequest, APIMessage, LoginRequest, RefreshRequest, TokenPair, UserPublic
from app.services.auth_service import login_user, refresh_access_token, register_admin, revoke_refresh_token

router = APIRouter()


@router.post("/admin/register", response_model=UserPublic)
def admin_register(payload: AdminRegisterRequest, db: DbSession) -> UserPublic:
    return register_admin(
        db,
        email=str(payload.email),
        password=payload.password,
        workspace_name=payload.workspace_name,
        display_name=payload.display_name,
    )


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, db: DbSession) -> TokenPair:
    user, access_token, refresh_token = login_user(
        db,
        username=payload.username,
        password=payload.password,
        device_id=payload.device_id,
        app_version=payload.app_version,
    )
    return TokenPair(access_token=access_token, refresh_token=refresh_token, user=user)


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    user, access_token, refresh_token = refresh_access_token(db, payload.refresh_token)
    return TokenPair(access_token=access_token, refresh_token=refresh_token, user=user)


@router.post("/logout", response_model=APIMessage)
def logout(payload: RefreshRequest, db: DbSession) -> APIMessage:
    revoke_refresh_token(db, payload.refresh_token)
    return APIMessage(message="ok")


@router.get("/me", response_model=UserPublic)
def me(current_user: CurrentUser) -> UserPublic:
    return current_user

