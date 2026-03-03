"""FastAPI application – full pipeline + approval workflow.

Webhook → filter → classify → context → draft → store.
Approval endpoints for human-in-the-loop review.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from src.models.webhook import JiraWebhookEvent
from src.models.comment import Comment
from src.models.draft import DraftStatus
from src.api.event_filter import EventFilter
from src.agent.classifier import CommentClassifier
from src.agent.drafter import ResponseDrafter

logger = logging.getLogger(__name__)

# ---- singletons ----------------------------------------------------- #

event_filter = EventFilter()

# Copilot SDK API key — optional; leave empty for keyword-only mode
_COPILOT_API_KEY: Optional[str] = os.getenv("COPILOT_API_KEY")
_COPILOT_MODEL: str = os.getenv("COPILOT_MODEL", "gpt-4")

classifier = CommentClassifier(api_key=_COPILOT_API_KEY, model=_COPILOT_MODEL)
drafter = ResponseDrafter(api_key=_COPILOT_API_KEY, model=_COPILOT_MODEL)

# In-memory draft store (MVP v1)
draft_store: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Application lifespan — startup / shutdown."""
    logger.info("Starting Jira Comment Assistant API (v0.3.0)")
    yield


app = FastAPI(
    title="Jira Comment Assistant",
    description="AI assistant for responding to Jira defect comments",
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.3.0",
        "drafts_in_store": len(draft_store),
    }

#  Webhook endpoint     
@app.post("/webhook/jira")
async def jira_webhook(request: Request):
    """
    Webhook endpoint for Jira events.

    Full pipeline:
    1. Parse & validate payload → JiraWebhookEvent.
    2. EventFilter gates (type, status, keyword, idempotency).
    3. Build Comment model from event.
    4. Classify → collect context → draft response → store.
    5. Return draft summary.
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

    # --- orchestrate -------------------------------------------------- #
    return await handle_comment_event(event)


# ===================================================================== #
#  Orchestration                                                         #
# ===================================================================== #

async def handle_comment_event(event: JiraWebhookEvent):
    """
    Full MVP v1 pipeline:
      Comment → Classify → Context → Draft → Store
    """
    assert event.comment is not None
    assert event.issue is not None

    # 1. Build Comment model from webhook event
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

    # 2. Classify
    classification = classifier.classify(comment)
    logger.info(
        "Classified %s comment %s → %s (%.2f)",
        comment.issue_key,
        comment.comment_id,
        classification.comment_type.value,
        classification.confidence,
    )

    # 3. Context collection (deferred if Jira creds not configured)
    context = _collect_context_safe(comment.issue_key)

    # 4. Draft response
    draft = drafter.draft(comment, classification, context)
    logger.info("Generated draft %s for %s", draft.draft_id, comment.issue_key)

    # 5. Store
    draft_store[draft.draft_id] = draft.model_dump(mode="json")

    return {
        "status": "processed",
        "event_id": event.event_id,
        "issue_key": comment.issue_key,
        "comment_id": comment.comment_id,
        "classification": classification.comment_type.value,
        "confidence": classification.confidence,
        "draft_id": draft.draft_id,
    }


def _collect_context_safe(issue_key: str):
    """Try to collect context from Jira; return minimal stub on failure."""
    try:
        from src.agent.context_collector import ContextCollector

        collector = ContextCollector()
        return collector.collect(issue_key)
    except Exception as exc:
        logger.warning("Context collection skipped (%s) – using stub", exc)
        from src.models.context import IssueContext, ContextCollectionResult

        return ContextCollectionResult(
            issue_context=IssueContext(
                issue_key=issue_key,
                summary="",
                description="",
                issue_type="Bug",
                status="Open",
                priority="Medium",
            ),
            collection_timestamp=datetime.now(timezone.utc),
            collection_duration_ms=0.0,
        )

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

#  Approval endpoint
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
