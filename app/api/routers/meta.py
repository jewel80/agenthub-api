"""Meta / observability endpoints (internal)."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, status

from app.core.config import settings
from app.services.observability import get_usage_tracker

router = APIRouter()


def _require_admin_token(x_admin_token: str | None) -> None:
    """Guard internal endpoints in production (fix-doc F13).

    Unknown/absent token -> 404 so the endpoint's existence is not revealed.
    Development/staging keep the endpoint open for local debugging.
    """
    if settings.ENVIRONMENT != "production":
        return
    admin_token = settings.ADMIN_TOKEN
    if not admin_token or not secrets.compare_digest(
        x_admin_token or "", admin_token
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


@router.get("/meta/usage", summary="Agent usage stats (basic observability)")
async def usage(x_admin_token: str | None = Header(default=None)) -> dict[str, object]:
    """Aggregate chat counts per agent/sub-agent since process start."""
    _require_admin_token(x_admin_token)
    return get_usage_tracker().snapshot()
