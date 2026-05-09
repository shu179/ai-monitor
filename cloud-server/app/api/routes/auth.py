from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas import (
    AdminRegisterRequest,
    APIMessage,
    LoginRequest,
    RefreshRequest,
    RequestPasswordResetRequest,
    ResendEmailVerificationRequest,
    ResetPasswordRequest,
    TokenPair,
    UserPublic,
    UpdateMyProfileRequest,
    VerifyEmailRequest,
)
from app.services.auth_service import (
    login_user,
    refresh_access_token,
    register_admin,
    request_password_reset,
    resend_admin_verification_code,
    reset_admin_password,
    revoke_refresh_token,
    update_my_profile,
    verify_admin_email,
)

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


@router.post("/email/verify", response_model=TokenPair)
def verify_email(payload: VerifyEmailRequest, db: DbSession) -> TokenPair:
    user, access_token, refresh_token = verify_admin_email(
        db,
        email=str(payload.email),
        code=payload.code,
        device_id=payload.device_id,
        app_version=payload.app_version,
    )
    return TokenPair(access_token=access_token, refresh_token=refresh_token, user=user)


@router.post("/email/resend", response_model=APIMessage)
def resend_email_verification(payload: ResendEmailVerificationRequest, db: DbSession) -> APIMessage:
    resend_admin_verification_code(db, email=str(payload.email))
    return APIMessage(message="ok")


@router.post("/password/reset/request", response_model=APIMessage)
def request_password_reset_code(payload: RequestPasswordResetRequest, db: DbSession) -> APIMessage:
    request_password_reset(db, email=str(payload.email))
    return APIMessage(message="ok")


@router.post("/password/reset/confirm", response_model=APIMessage)
def confirm_password_reset(payload: ResetPasswordRequest, db: DbSession) -> APIMessage:
    reset_admin_password(
        db,
        email=str(payload.email),
        code=payload.code,
        password=payload.password,
    )
    return APIMessage(message="ok")


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


@router.patch("/me/profile", response_model=UserPublic)
def update_profile(payload: UpdateMyProfileRequest, current_user: CurrentUser, db: DbSession) -> UserPublic:
    return update_my_profile(
        db,
        current_user,
        display_name=payload.display_name,
        avatar=payload.avatar,
        birthday=payload.birthday,
        hire_date=payload.hire_date,
        fields_set=set(payload.model_fields_set),
    )
