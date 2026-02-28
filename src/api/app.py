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

from src.models.webhook import JiraWebhookEvent
from src.models.comment import Comment
from src.api.event_filter import EventFilter

logger = logging.getLogger(__name__)

# ---- singletons ----------------------------------------------------- #

event_filter = EventFilter()


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Application lifespan — startup / shutdown."""
    logger.info("Starting Jira Comment Assistant API (v0.1.0)")
    yield


app = FastAPI(
    title="Jira Comment Assistant",
    description="AI assistant for responding to Jira defect comments",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
    }


# ===================================================================== #
#  Webhook endpoint                                                      #
# ===================================================================== #

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
