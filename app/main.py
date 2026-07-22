"""FastAPI application factory.

Routers are registered here as they are built (agents catalog → auth → chat).
Phase 0 ships only `/health` + CORS; the rest is added in subsequent phases.
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
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
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        _request_logger.info(
            "%s %s -> %s (%.0fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.APP_NAME}

    # Routers are mounted in later phases:
    #   app.include_router(agents.router)
    #   app.include_router(auth.router)
    #   app.include_router(chat.router)
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
