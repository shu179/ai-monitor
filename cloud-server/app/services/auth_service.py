from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException, status
from jwt import InvalidTokenError
from sqlalchemy import select
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
from app.models import DeviceSession, RefreshToken, User, UserRole, Workspace


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
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Admin email already registered")

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
    return user


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

