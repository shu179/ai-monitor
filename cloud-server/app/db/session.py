from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=max(1, int(settings.db_pool_size)),
    max_overflow=max(0, int(settings.db_max_overflow)),
    pool_recycle=max(1, int(settings.db_pool_recycle_seconds)),
    pool_timeout=max(1, int(settings.db_pool_timeout_seconds)),
    connect_args={
        "options": f"-c statement_timeout={max(0, int(settings.db_statement_timeout_ms))}",
    },
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
