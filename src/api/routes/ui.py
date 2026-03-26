"""Draft Review UI routes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.api.deps import draft_store, _jira_client
from src.models.draft import DraftStatus
from src.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

# Templates directory — src/api/templates/
_ui_dir = Path(__file__).parent.parent  # src/api/
_templates = Jinja2Templates(directory=str(_ui_dir / "templates"))


@router.get("/ui")
async def ui_list(
    request: Request,
    issue_key: Optional[str] = None,
    status: Optional[str] = None,
):
    """Draft list page — shows all stored drafts with optional filters."""
    drafts = draft_store.list_all(issue_key=issue_key, status=status, limit=100)
    total = draft_store.count(issue_key=issue_key, status=status)
    return _templates.TemplateResponse(
        request,
        "drafts.html",
        {
            "drafts": drafts,
            "total": total,
            "filters": {"issue_key": issue_key, "status": status},
        },
    )


@router.get("/ui/drafts/{draft_id}")
async def ui_review(request: Request, draft_id: str):
    """Draft review page — shows full draft with evidence panel and actions."""
    draft = draft_store.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")

    jira_url = f"{settings.jira.base_url}/browse/{draft.get('issue_key', '')}"
    missing_context: list[str] = draft.get("missing_info") or []

    return _templates.TemplateResponse(
        request,
        "draft_review.html",
        {
            "draft": draft,
            "jira_url": jira_url,
            "missing_context": missing_context,
        },
    )


@router.post("/ui/drafts/{draft_id}/approve")
async def ui_approve(
    request: Request,
    draft_id: str,
    body: Optional[str] = Form(default=None),
):
    """Handle approve form submission — optionally update body, then approve."""
    draft_data = draft_store.get(draft_id)
    if draft_data is None:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Persist edited body if the human changed it
    effective_body = (
        body.strip() if body and body.strip() else draft_data.get("body", "")
    )
    if effective_body != draft_data.get("body", ""):
        draft_store.update_body(draft_id, effective_body)

    draft_store.update_status(draft_id, DraftStatus.APPROVED, approved_by="ui")

    if _jira_client is not None:
        try:
            _jira_client.add_comment(draft_data["issue_key"], effective_body)
            draft_store.mark_posted(draft_id)
            logger.info("Draft %s posted to Jira via UI", draft_id)
        except Exception as exc:
            logger.error("Failed to post draft %s to Jira: %s", draft_id, exc)

    return RedirectResponse(url=f"/ui/drafts/{draft_id}", status_code=303)


@router.post("/ui/drafts/{draft_id}/reject")
async def ui_reject(
    request: Request,
    draft_id: str,
    feedback: str = Form(default=""),
):
    """Handle reject form submission."""
    draft_data = draft_store.get(draft_id)
    if draft_data is None:
        raise HTTPException(status_code=404, detail="Draft not found")

    draft_store.update_status(draft_id, DraftStatus.REJECTED, feedback=feedback)
    return RedirectResponse(url=f"/ui/drafts/{draft_id}", status_code=303)


@router.post("/ui/clear-all")
async def ui_clear_all(request: Request):
    """Delete every draft from the local database."""
    deleted = draft_store.clear()
    logger.info("Cleared all drafts from UI — %d removed", deleted)
    return RedirectResponse(url="/ui", status_code=303)


@router.post("/ui/drafts/{draft_id}/rate")
async def rate_draft(draft_id: str, request: Request):
    """Rate a draft (1–5 stars)."""
    try:
        payload = await request.json()
        rating = payload.get("rating")
        if not isinstance(rating, int) or not (1 <= rating <= 5):
            raise HTTPException(
                status_code=400, detail="'rating' must be an integer 1–5"
            )
        ok = draft_store.save_rating(draft_id, rating)
        if not ok:
            raise HTTPException(status_code=404, detail="Draft not found")
        return {"status": "rated", "draft_id": draft_id, "rating": rating}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error rating draft %s: %s", draft_id, exc)
        raise HTTPException(status_code=500, detail="Failed to rate draft")


# Polling endpoints for auto-refresh

@router.get("/ui/api/drafts")
async def ui_api_drafts(
    issue_key: Optional[str] = None,
    status: Optional[str] = None,
):
    """JSON endpoint for draft list polling (auto-refresh)."""
    drafts = draft_store.list_all(issue_key=issue_key, status=status, limit=100)
    total = draft_store.count(issue_key=issue_key, status=status)
    return JSONResponse({"drafts": drafts, "total": total})


@router.get("/ui/api/drafts/{draft_id}")
async def ui_api_draft_detail(draft_id: str):
    """JSON endpoint for single-draft polling (auto-refresh review page)."""
    draft = draft_store.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return JSONResponse(draft)
