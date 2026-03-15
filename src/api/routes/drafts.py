"""Draft retrieval and rejection routes.

NOTE: POST /approve is intentionally absent from this module.
It lives in app.py so that ``@patch("src.api.app._jira_client")`` in the
test suite continues to work — Python mock patching replaces the name
binding in the module where the handler is defined.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.api.deps import draft_store, notifier
from src.models.draft import DraftStatus

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: str):
    """Retrieve a stored draft by ID."""
    draft = draft_store.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@router.get("/drafts")
async def list_drafts(
    issue_key: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    """List all drafts, optionally filtered by issue_key and/or status."""
    drafts = draft_store.list_all(
        issue_key=issue_key, status=status, limit=limit, offset=offset
    )
    total = draft_store.count(issue_key=issue_key, status=status)
    return {"count": len(drafts), "total": total, "drafts": drafts}


@router.post("/reject")
async def reject_draft(request: Request):
    """Reject a draft response with optional feedback."""
    try:
        payload = await request.json()
        draft_id = payload.get("draft_id")
        feedback = payload.get("feedback", "")

        draft_data = draft_store.get(draft_id)
        if draft_data is None:
            raise HTTPException(status_code=404, detail="Draft not found")

        updated = draft_store.update_status(
            draft_id, DraftStatus.REJECTED, feedback=feedback
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Draft not found")

        logger.info("Draft %s rejected. Feedback: %s", draft_id, feedback)

        notifier.notify_draft_rejected(
            draft_id=draft_id,
            issue_key=draft_data.get("issue_key", ""),
            feedback=feedback,
        )

        return {"status": "rejected", "draft_id": draft_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error rejecting draft: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to reject draft")
