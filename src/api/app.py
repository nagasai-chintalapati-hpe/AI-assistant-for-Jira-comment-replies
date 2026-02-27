"""FastAPI application – Phase 1: Architecture & Scaffolding.

Webhook receiver → event filtering → accepted event logging.
Classification, drafting, and notifications are added in later phases.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import logging
from datetime import datetime, timezone
from typing import Optional

from src.models.webhook import JiraWebhookEvent
from src.models.comment import Comment
from src.models.draft import DraftStatus
from src.api.event_filter import EventFilter

logger = logging.getLogger(__name__)

# ---- singletons ----------------------------------------------------- #

event_filter = EventFilter()

# In-memory draft store (MVP v1)
draft_store: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Application lifespan — startup / shutdown."""
    logger.info("Starting Jira Comment Assistant API (v0.2.0)")
    yield


app = FastAPI(
    title="Jira Comment Assistant",
    description="AI assistant for responding to Jira defect comments",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.2.0",
        "drafts_in_store": len(draft_store),
    }

#  Webhook endpoint     
@app.post("/webhook/jira")
async def jira_webhook(request: Request):
    """
    Webhook endpoint for Jira events.

    Phase 1 flow:
    1. Parse & validate payload → JiraWebhookEvent.
    2. EventFilter gates (type, status, keyword, idempotency).
    3. Build Comment model from accepted event.
    4. Return accepted event summary (classification added in Phase 2).
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # --- parse -------------------------------------------------------- #
    try:
        event = JiraWebhookEvent(**payload)
    except ValidationError as exc:
        logger.warning("Webhook payload validation failed: %s", exc.errors())
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": "Payload validation failed"},
        )

    logger.info(
        "Received webhook | event=%s issue=%s comment=%s",
        event.webhookEvent,
        event.issue_key,
        event.comment.id if event.comment else None,
    )

    # --- filter ------------------------------------------------------- #
    result = event_filter.evaluate(event)
    if not result.accepted:
        logger.info("Event filtered out: %s", result.reason)
        return {
            "status": "filtered",
            "reason": result.reason,
            "event_id": result.event_id,
        }

    # --- build Comment model ------------------------------------------ #
    assert event.comment is not None
    assert event.issue is not None

    comment = Comment(
        comment_id=event.comment.id,
        issue_key=event.issue.key,
        author=(
            event.comment.author.displayName
            or event.comment.author.emailAddress
            or "unknown"
        ),
        created=datetime.fromisoformat(
            event.comment.created.replace("+0000", "+00:00")
        ) if event.comment.created else datetime.now(timezone.utc),
        updated=datetime.fromisoformat(
            event.comment.updated.replace("+0000", "+00:00")
        ) if event.comment.updated else datetime.now(timezone.utc),
        body=event.comment.body,
    )

    logger.info(
        "Accepted comment %s on %s by %s",
        comment.comment_id,
        comment.issue_key,
        comment.author,
    )

    # TODO (Phase 2): classify comment
    # TODO (Phase 3): collect context & draft response

    return {
        "status": "accepted",
        "event_id": event.event_id,
        "issue_key": comment.issue_key,
        "comment_id": comment.comment_id,
    }

#  Draft retrieval   
@app.get("/drafts/{draft_id}")
async def get_draft(draft_id: str):
    """Retrieve a stored draft by ID."""
    draft = draft_store.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@app.get("/drafts")
async def list_drafts(issue_key: Optional[str] = None):
    """List all drafts, optionally filtered by issue_key."""
    drafts = list(draft_store.values())
    if issue_key:
        drafts = [d for d in drafts if d.get("issue_key") == issue_key]
    return {"count": len(drafts), "drafts": drafts}


# ===================================================================== #
#  Approval endpoints                                                    #
# ===================================================================== #

@app.post("/approve")
async def approve_draft(request: Request):
    """
    Approve a draft response.
    On approval the draft is marked and (optionally) posted to Jira.
    """
    try:
        payload = await request.json()
        draft_id = payload.get("draft_id")
        approved_by = payload.get("approved_by")

        if draft_id not in draft_store:
            raise HTTPException(status_code=404, detail="Draft not found")

        draft_store[draft_id]["status"] = DraftStatus.APPROVED.value
        draft_store[draft_id]["approved_by"] = approved_by
        draft_store[draft_id]["approved_at"] = datetime.now(timezone.utc).isoformat()

        logger.info("Draft %s approved by %s", draft_id, approved_by)

        # TODO: Post comment to Jira via JiraClient.add_comment()

        return {"status": "approved", "draft_id": draft_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error approving draft: %s", e)
        raise HTTPException(status_code=500, detail="Failed to approve draft")


@app.post("/reject")
async def reject_draft(request: Request):
    """Reject a draft response with optional feedback."""
    try:
        payload = await request.json()
        draft_id = payload.get("draft_id")
        feedback = payload.get("feedback", "")

        if draft_id not in draft_store:
            raise HTTPException(status_code=404, detail="Draft not found")

        draft_store[draft_id]["status"] = DraftStatus.REJECTED.value
        draft_store[draft_id]["feedback"] = feedback

        logger.info("Draft %s rejected. Feedback: %s", draft_id, feedback)

        return {"status": "rejected", "draft_id": draft_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error rejecting draft: %s", e)
        raise HTTPException(status_code=500, detail="Failed to reject draft")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
