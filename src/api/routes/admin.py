"""Admin maintenance routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from src.api.deps import draft_store

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/admin/drafts/purge-stale")
async def purge_stale_drafts(request: Request):
    """Delete unactioned GENERATED drafts older than N days."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    days: int = int(payload.get("days", 30))
    if days < 1:
        raise HTTPException(status_code=400, detail="'days' must be >= 1")

    deleted = draft_store.purge_stale(days=days)
    return {
        "status": "purged",
        "deleted_drafts": deleted,
        "older_than_days": days,
    }
