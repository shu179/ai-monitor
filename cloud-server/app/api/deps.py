from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.db.session import get_db
from app.models import User, UserRole


DbSession = Annotated[Session, Depends(get_db)]
bearer_scheme = HTTPBearer(auto_error=False)


def _bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
    token = credentials.credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header")
    return token


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    token = _bearer_token(credentials)
    try:
        payload = decode_token(token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    user_id = int(payload.get("sub") or 0)
    user = db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if not user or not user.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User disabled")
    if int(payload.get("token_version") or 0) != user.token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(current_user: CurrentUser) -> User:
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]
