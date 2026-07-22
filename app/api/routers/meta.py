"""Meta / observability endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from app.services.observability import get_usage_tracker

router = APIRouter()


@router.get("/meta/usage", summary="Agent usage stats (basic observability)")
async def usage() -> dict[str, object]:
    """Aggregate chat counts per agent/sub-agent since process start."""
    return get_usage_tracker().snapshot()
