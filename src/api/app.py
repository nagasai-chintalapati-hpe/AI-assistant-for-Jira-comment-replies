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
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Optional

from src.models.webhook import JiraWebhookEvent
from src.models.comment import Comment
from src.models.draft import DraftStatus
from src.api.event_filter import EventFilter
from src.agent.classifier import CommentClassifier
from src.agent.drafter import ResponseDrafter
from src.storage import SQLiteStore

logger = logging.getLogger(__name__)

_ENV = os.getenv("ENV", "development").lower()
_WEBHOOK_SECRET: Optional[str] = os.getenv("WEBHOOK_SECRET")
_APPROVAL_API_KEY: Optional[str] = os.getenv("APPROVAL_API_KEY")
_DB_PATH = os.getenv("ASSISTANT_DB_PATH", ".data/assistant.db")
_draft_backend = SQLiteStore(_DB_PATH)


class PersistentDraftStore:
    """Dict-like wrapper backed by SQLite for compatibility with existing tests."""

    def __init__(self, backend: SQLiteStore):
        self._backend = backend

    def __setitem__(self, draft_id: str, value: dict) -> None:
        self._backend.upsert_draft(value)

    def __getitem__(self, draft_id: str) -> dict:
        value = self._backend.get_draft(draft_id)
        if value is None:
            raise KeyError(draft_id)
        return value

    def __contains__(self, draft_id: str) -> bool:
        return self._backend.get_draft(draft_id) is not None

    def __len__(self) -> int:
        return len(self._backend.list_drafts())

    def get(self, draft_id: str) -> Optional[dict]:
        return self._backend.get_draft(draft_id)

    def values(self) -> list[dict]:
        return self._backend.list_drafts()

    def clear(self) -> None:
        self._backend.clear_drafts()

# ---- singletons ----------------------------------------------------- #

event_filter = EventFilter(event_store=_draft_backend)

# Copilot SDK configuration (optional; uses keywords fallback if not provided)
_COPILOT_API_KEY: Optional[str] = os.getenv("COPILOT_API_KEY")
_COPILOT_MODEL: str = os.getenv("COPILOT_MODEL", "claude-sonnet-4.5")

classifier = CommentClassifier(api_key=_COPILOT_API_KEY, model=_COPILOT_MODEL)
drafter = ResponseDrafter(api_key=_COPILOT_API_KEY, model=_COPILOT_MODEL)

# Persistent draft store
draft_store = PersistentDraftStore(_draft_backend)


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Application lifespan — startup / shutdown."""
    if _ENV == "production":
        if not _WEBHOOK_SECRET:
            raise RuntimeError("WEBHOOK_SECRET is required in production")
        if not _APPROVAL_API_KEY:
            raise RuntimeError("APPROVAL_API_KEY is required in production")
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
    body = await request.body()
    _verify_webhook_signature(request, body)

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
    classification = await classifier.classify(comment)
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
    draft = await drafter.draft(comment, classification, context)
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


def _verify_webhook_signature(request: Request, body: bytes) -> None:
    """Validate webhook HMAC signature when WEBHOOK_SECRET is configured."""
    if not _WEBHOOK_SECRET:
        return

    provided = request.headers.get("x-hub-signature-256") or request.headers.get(
        "x-webhook-signature"
    )
    if not provided:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    digest = hmac.new(_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    is_valid = hmac.compare_digest(provided, expected) or hmac.compare_digest(provided, digest)
    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def _verify_approval_auth(request: Request) -> None:
    """Protect approve/reject endpoints with a shared token when configured."""
    if not _APPROVAL_API_KEY:
        return

    provided = request.headers.get("x-approval-token")
    if not provided:
        raise HTTPException(status_code=401, detail="Missing approval token")
    if not hmac.compare_digest(provided, _APPROVAL_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid approval token")


def _post_approved_draft_to_jira(draft: dict) -> dict:
    """Try posting approved draft to Jira and return outcome metadata."""
    try:
        from src.integrations.jira import JiraClient

        issue_key = draft.get("issue_key")
        body = draft.get("body", "")
        if not issue_key or not body:
            return {
                "posted_to_jira": False,
                "jira_comment_id": None,
                "post_reason": "missing issue key or body",
            }

        client = JiraClient()
        comment_id = client.add_comment(issue_key=issue_key, comment_body=body)
        return {
            "posted_to_jira": True,
            "jira_comment_id": comment_id,
            "post_reason": None,
        }
    except Exception as exc:
        logger.warning("Jira post skipped/failed for draft %s: %s", draft.get("draft_id"), exc)
        return {
            "posted_to_jira": False,
            "jira_comment_id": None,
            "post_reason": str(exc),
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

#  Approval endpoint
@app.post("/approve")
async def approve_draft(request: Request):
    """
    Approve a draft response.
    On approval the draft is marked and (optionally) posted to Jira.
    """
    try:
        _verify_approval_auth(request)
        payload = await request.json()
        draft_id = payload.get("draft_id")
        approved_by = payload.get("approved_by")

        if draft_id not in draft_store:
            raise HTTPException(status_code=404, detail="Draft not found")

        draft = draft_store.get(draft_id)
        assert draft is not None
        draft["status"] = DraftStatus.APPROVED.value
        draft["approved_by"] = approved_by
        draft["approved_at"] = datetime.now(timezone.utc).isoformat()

        post_result = _post_approved_draft_to_jira(draft)
        if post_result["posted_to_jira"]:
            draft["status"] = DraftStatus.POSTED.value
            draft["posted_at"] = datetime.now(timezone.utc).isoformat()
            draft["jira_comment_id"] = post_result["jira_comment_id"]

        draft_store[draft_id] = draft

        logger.info("Draft %s approved by %s", draft_id, approved_by)

        return {
            "status": "approved",
            "draft_id": draft_id,
            **post_result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error approving draft: %s", e)
        raise HTTPException(status_code=500, detail="Failed to approve draft")


@app.post("/reject")
async def reject_draft(request: Request):
    """Reject a draft response with optional feedback."""
    try:
        _verify_approval_auth(request)
        payload = await request.json()
        draft_id = payload.get("draft_id")
        feedback = payload.get("feedback", "")

        if draft_id not in draft_store:
            raise HTTPException(status_code=404, detail="Draft not found")

        draft = draft_store.get(draft_id)
        assert draft is not None
        draft["status"] = DraftStatus.REJECTED.value
        draft["feedback"] = feedback
        draft_store[draft_id] = draft

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
