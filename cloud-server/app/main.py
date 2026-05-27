from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.router import api_router, api_v2_router
from app.core.config import get_settings

logger = logging.getLogger(__name__)
SLOW_REQUEST_MS = 200


class TraceRequestMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        trace_id = _normalize_trace_id(request.headers.get("X-Trace-Id")) or f"cloud-{uuid.uuid4().hex[:24]}"
        started_at = time.monotonic()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed_ms = int(round((time.monotonic() - started_at) * 1000))
            status_code = getattr(response, "status_code", 500)
            if response is not None:
                response.headers["X-Trace-Id"] = trace_id
            if elapsed_ms >= SLOW_REQUEST_MS or str(request.url.path).startswith("/api/v2/sync"):
                logger.info(
                    "[CloudAPI] request trace_id=%s method=%s path=%s status=%s elapsed_ms=%s",
                    trace_id,
                    request.method,
                    request.url.path,
                    status_code,
                    elapsed_ms,
                )


def _normalize_trace_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "".join(ch for ch in text if ch.isalnum() or ch in {"-", "_", "."})[:128]


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Surfaced Cloud Server",
        version="0.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(TraceRequestMiddleware)
    app.include_router(api_router)
    app.include_router(api_v2_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "surfaced-cloud"}

    return app


app = create_app()
