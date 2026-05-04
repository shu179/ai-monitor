from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import HTTPException, status
from jwt import InvalidTokenError
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    new_token_jti,
    utc_now,
    verify_password,
)
from app.models import DeviceSession, EmailVerificationCode, RefreshToken, User, UserRole, Workspace
from app.services.email_service import EmailDeliveryError, send_verification_code_email

EMAIL_PURPOSE_ADMIN_REGISTER = "admin_register"
EMAIL_PURPOSE_PASSWORD_RESET = "password_reset"


def register_admin(
    db: Session,
    *,
    email: str,
    password: str,
    workspace_name: str,
    display_name: str | None,
) -> User:
    settings = get_settings()
    if not settings.admin_self_register_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin self registration is disabled")
    normalized_email = email.strip().lower()
    existing = db.scalar(select(User).where(User.email == normalized_email, User.role == UserRole.admin))
    if existing:
        if not settings.email_verification_required or existing.email_verified:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Admin email already registered")
        existing.password_hash = hash_password(password)
        existing.display_name = display_name or workspace_name.strip()
        existing.workspace.name = workspace_name.strip()
        db.commit()
        db.refresh(existing)
        send_admin_verification_code(db, user=existing)
        return existing

    workspace = Workspace(name=workspace_name.strip())
    db.add(workspace)
    db.flush()
    user = User(
        workspace_id=workspace.id,
        username=normalized_email,
        email=normalized_email,
        email_verified=not settings.email_verification_required,
        password_hash=hash_password(password),
        role=UserRole.admin,
        display_name=display_name or workspace_name.strip(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    if settings.email_verification_required:
        send_admin_verification_code(db, user=user)
    return user


def send_admin_verification_code(db: Session, *, user: User) -> None:
    send_user_email_code(
        db,
        user=user,
        purpose=EMAIL_PURPOSE_ADMIN_REGISTER,
        skip_if_verified=True,
    )


def send_user_email_code(
    db: Session,
    *,
    user: User,
    purpose: str,
    skip_if_verified: bool = False,
) -> None:
    settings = get_settings()
    if user.role != UserRole.admin or not user.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admin email is required")
    if skip_if_verified and user.email_verified:
        return

    latest = db.scalar(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.user_id == user.id,
            EmailVerificationCode.purpose == purpose,
            EmailVerificationCode.consumed_at.is_(None),
        )
        .order_by(desc(EmailVerificationCode.created_at), desc(EmailVerificationCode.id))
    )
    now = utc_now()
    if latest and (now - latest.last_sent_at).total_seconds() < settings.email_code_resend_seconds:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Please wait before requesting another code")

    code = f"{secrets.randbelow(1_000_000):06d}"
    row = EmailVerificationCode(
        user_id=user.id,
        email=user.email.strip().lower(),
        code_hash=hash_password(code),
        purpose=purpose,
        expires_at=now + timedelta(minutes=settings.email_code_ttl_minutes),
        last_sent_at=now,
    )
    db.add(row)
    try:
        send_verification_code_email(to_email=user.email, code=code)
    except EmailDeliveryError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    db.commit()


def resend_admin_verification_code(db: Session, *, email: str) -> None:
    normalized_email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized_email, User.role == UserRole.admin))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin email is not registered")
    send_admin_verification_code(db, user=user)


def request_password_reset(db: Session, *, email: str) -> None:
    normalized_email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized_email, User.role == UserRole.admin))
    if not user or not user.enabled or not user.email_verified:
        return
    send_user_email_code(db, user=user, purpose=EMAIL_PURPOSE_PASSWORD_RESET)


def reset_admin_password(db: Session, *, email: str, code: str, password: str) -> None:
    normalized_email = email.strip().lower()
    normalized_code = "".join(ch for ch in str(code or "") if ch.isdigit())
    user = db.scalar(select(User).where(User.email == normalized_email, User.role == UserRole.admin))
    if not user or not user.enabled or not user.email_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password reset code is invalid or expired")
    row = _latest_email_code(db, user=user, email=normalized_email, purpose=EMAIL_PURPOSE_PASSWORD_RESET)
    _consume_email_code_or_raise(db, row=row, code=normalized_code, invalid_detail="Password reset code is invalid or expired")

    now = utc_now()
    user.password_hash = hash_password(password)
    user.token_version += 1
    user.updated_at = now
    db.query(RefreshToken).filter(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False)).update(
        {"revoked": True},
        synchronize_session=False,
    )
    db.commit()


def verify_admin_email(
    db: Session,
    *,
    email: str,
    code: str,
    device_id: str | None = None,
    app_version: str | None = None,
) -> tuple[User, str, str]:
    normalized_email = email.strip().lower()
    normalized_code = "".join(ch for ch in str(code or "") if ch.isdigit())
    user = db.scalar(select(User).where(User.email == normalized_email, User.role == UserRole.admin))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin email is not registered")
    if user.email_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already verified")
    row = _latest_email_code(db, user=user, email=normalized_email, purpose=EMAIL_PURPOSE_ADMIN_REGISTER)
    _consume_email_code_or_raise(db, row=row, code=normalized_code)
    user.email_verified = True
    db.commit()
    db.refresh(user)
    return _issue_login_session(db, user, device_id=device_id, app_version=app_version)


def _latest_email_code(
    db: Session,
    *,
    user: User,
    email: str,
    purpose: str,
) -> EmailVerificationCode | None:
    return db.scalar(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.user_id == user.id,
            EmailVerificationCode.email == email,
            EmailVerificationCode.purpose == purpose,
            EmailVerificationCode.consumed_at.is_(None),
        )
        .order_by(desc(EmailVerificationCode.created_at), desc(EmailVerificationCode.id))
    )


def _consume_email_code_or_raise(
    db: Session,
    *,
    row: EmailVerificationCode | None,
    code: str,
    invalid_detail: str = "Invalid verification code",
) -> None:
    if not row:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification code is missing or expired")
    now = utc_now()
    settings = get_settings()
    if row.expires_at < now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification code expired")
    if row.attempts >= settings.email_code_max_attempts:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many verification attempts")

    row.attempts += 1
    if not code or not verify_password(code, row.code_hash):
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=invalid_detail)

    row.consumed_at = now
    db.commit()


def issue_token_pair(db: Session, user: User) -> tuple[str, str]:
    settings = get_settings()
    jti = new_token_jti()
    refresh_row = RefreshToken(
        user_id=user.id,
        jti=jti,
        expires_at=utc_now() + timedelta(days=settings.refresh_token_days),
    )
    db.add(refresh_row)
    user.last_login_at = utc_now()
    db.commit()
    return (
        create_access_token(
            user_id=user.id,
            workspace_id=user.workspace_id,
            role=user.role.value,
            token_version=user.token_version,
        ),
        create_refresh_token(user_id=user.id, jti=jti),
    )


def _issue_login_session(
    db: Session,
    user: User,
    *,
    device_id: str | None = None,
    app_version: str | None = None,
) -> tuple[User, str, str]:
    if device_id:
        session = db.scalar(select(DeviceSession).where(DeviceSession.user_id == user.id, DeviceSession.device_id == device_id))
        if not session:
            session = DeviceSession(user_id=user.id, device_id=device_id, app_version=app_version)
            db.add(session)
        else:
            session.app_version = app_version
            session.last_seen_at = utc_now()
    access_token, refresh_token = issue_token_pair(db, user)
    db.refresh(user)
    return user, access_token, refresh_token


def login_user(
    db: Session,
    *,
    username: str,
    password: str,
    device_id: str | None = None,
    app_version: str | None = None,
) -> tuple[User, str, str]:
    normalized_username = username.strip().lower()
    user = db.scalar(select(User).where(User.username == normalized_username))
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    if not user.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User disabled")
    if user.role == UserRole.admin and not user.email_verified and get_settings().email_verification_required:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email is not verified")

    return _issue_login_session(db, user, device_id=device_id, app_version=app_version)


def refresh_access_token(db: Session, refresh_token: str) -> tuple[User, str, str]:
    try:
        payload = decode_token(refresh_token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from exc
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    user_id = int(payload.get("sub") or 0)
    jti = str(payload.get("jti") or "")
    token_row = db.scalar(select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.jti == jti))
    if not token_row or token_row.revoked or token_row.expires_at < utc_now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token revoked")
    user = db.scalar(select(User).where(User.id == user_id))
    if not user or not user.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User disabled")

    token_row.revoked = True
    access_token, new_refresh_token = issue_token_pair(db, user)
    db.refresh(user)
    return user, access_token, new_refresh_token


def revoke_refresh_token(db: Session, refresh_token: str) -> None:
    try:
        payload = decode_token(refresh_token)
    except InvalidTokenError:
        return
    jti = str(payload.get("jti") or "")
    if not jti:
        return
    token_row = db.scalar(select(RefreshToken).where(RefreshToken.jti == jti))
    if token_row:
        token_row.revoked = True
        db.commit()
