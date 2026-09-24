"""FastAPI application factory.

Routers are registered here as they are built (agents catalog → auth → chat).
`/health` is cheap liveness; `/health/ready` verifies database connectivity
with a short timeout (used by deploy platforms as the health check).
Every response carries an `X-Request-ID` (accepted or generated) which is
also embedded in all log lines and error bodies.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import db as core_db
from app.core.config import settings
from app.core.logging import request_id_var, setup_logging
from app.services.llm.base import LLMUnavailableError

# How long /health/ready waits for `SELECT 1` before reporting unready.
READY_CHECK_TIMEOUT_SECONDS = 2.0

setup_logging(settings.LOG_FORMAT)
_request_logger = logging.getLogger("agenthub.http")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        description=(
            "AgentHub — a multi-tenant 'Play Store for AI agents'. "
            "Agents are config/data, not code."
        ),
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Request-ID + access log (fix-doc F15).

        Accepts a client-supplied X-Request-ID or generates one; the id is
        returned on every response, added to error bodies, and available to
        all log lines via the request_id contextvar.
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            # Log while the contextvar is still set so the line carries the id.
            elapsed_ms = (time.perf_counter() - start) * 1000
            _request_logger.info(
                "%s %s -> %s (%.0fms)",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
            return response
        finally:
            request_id_var.reset(token)

    @app.get("/health", tags=["meta"], summary="Liveness (cheap)")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.APP_NAME}

    @app.get(
        "/health/ready",
        tags=["meta"],
        summary="Readiness (database reachable)",
    )
    async def readiness() -> dict[str, str]:
        """503 when the database is unreachable; 200 when it answers SELECT 1."""
        try:
            async with asyncio.timeout(READY_CHECK_TIMEOUT_SECONDS):
                async with core_db.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except TimeoutError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Database not ready (timeout)"
            ) from None
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Database not ready",
            ) from exc
        return {"status": "ready"}

    @app.exception_handler(LLMUnavailableError)
    async def llm_unavailable_handler(
        _request: Request, _exc: LLMUnavailableError
    ) -> JSONResponse:
        """LLM outage -> 503 + Retry-After (never a 500, never internals)."""
        return JSONResponse(
            status_code=503,
            content={
                "detail": "The assistant is temporarily unavailable. "
                "Please try again."
            },
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Default error shape + request_id (additive; `detail` unchanged)."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": request_id_var.get()},
            headers=getattr(exc, "headers", None),
        )

    _register_routers(app)
    return app


def _register_routers(app: FastAPI) -> None:
    """Mount routers incrementally — each loads independently if present."""
    import importlib

    for name in ("agents", "auth", "chat", "meta"):
        try:
            mod = importlib.import_module(f"app.api.routers.{name}")
        except ImportError:
            # Router not implemented yet (in-progress phase) — skip it.
            continue
        router = getattr(mod, "router", None)
        if router is not None:
            app.include_router(router)


app = create_app()
