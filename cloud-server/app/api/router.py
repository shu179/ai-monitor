from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import admin, auth, sync, tasks, updates

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(sync.router, prefix="/sync", tags=["sync"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(updates.router, prefix="/updates", tags=["updates"])
