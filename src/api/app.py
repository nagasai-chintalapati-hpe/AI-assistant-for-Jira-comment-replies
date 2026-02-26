"""FastAPI application for webhook and approval endpoints"""

from fastapi import FastAPI, HTTPException, Request
import logging
from datetime import datetime

from src.agent.classifier import CommentClassifier
from src.agent.context_collector import ContextCollector
from src.agent.drafter import ResponseDrafter
from src.models.comment import Comment
from src.models.context import ContextCollectionResult, IssueContext

app = FastAPI(
    title="Jira Comment Assistant",
    description="AI assistant for responding to Jira defect comments",
    version="0.1.0",
)

logger = logging.getLogger(__name__)


@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    logger.info("Starting Jira Comment Assistant API")


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/webhook/jira")
async def jira_webhook(request: Request):
    """
    Webhook endpoint for Jira events.
    Receives comment.created and comment.updated events.
    """
    try:
        payload = await request.json()
        event_type = payload.get("webhookEvent")
        
        logger.info(f"Received Jira webhook event: {event_type}")
        
        if event_type == "comment_created":
            return await handle_comment_created(payload)
        elif event_type == "comment_updated":
            return await handle_comment_updated(payload)
        else:
            return {"status": "ignored", "reason": f"Unhandled event: {event_type}"}
    
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def handle_comment_created(payload: dict):
    """Handle new comment created event"""
    return await _process_comment_event(payload, event_name="comment_created")


async def handle_comment_updated(payload: dict):
    """Handle comment updated event"""
    return await _process_comment_event(payload, event_name="comment_updated")


async def _process_comment_event(payload: dict, event_name: str) -> dict:
    comment = _build_comment_from_payload(payload)

    if not comment.issue_key or not comment.comment_id:
        return {
            "status": "ignored",
            "reason": "Missing issue key or comment id",
            "event": event_name,
        }

    classifier = CommentClassifier()
    context_collector = ContextCollector()
    drafter = ResponseDrafter()

    classification = classifier.classify(comment)
    context = _safe_collect_context(context_collector, comment)
    draft = drafter.draft(comment, classification, context)

    return {
        "status": "drafted",
        "event": event_name,
        "issue_key": draft.issue_key,
        "comment_id": draft.in_reply_to_comment_id,
        "draft_id": draft.draft_id,
        "confidence": draft.confidence_score,
        "suggested_labels": draft.suggested_labels,
        "body": draft.body,
    }


def _safe_collect_context(
    collector: ContextCollector,
    comment: Comment,
) -> ContextCollectionResult:
    try:
        return collector.collect(comment.issue_key)
    except Exception as exc:
        logger.warning(
            "Context collection failed for %s: %s",
            comment.issue_key,
            exc,
        )
        issue_context = IssueContext(
            issue_key=comment.issue_key,
            summary="",
            description="",
            issue_type="",
            status="",
            priority="",
        )
        return ContextCollectionResult(
            issue_context=issue_context,
            rag_results=[],
            available_logs=[],
            collection_timestamp=datetime.utcnow(),
            collection_duration_ms=0.0,
        )


def _build_comment_from_payload(payload: dict) -> Comment:
    issue = payload.get("issue", {}) or {}
    comment_data = payload.get("comment", {}) or {}
    author = comment_data.get("author", {}) or {}

    created = _parse_timestamp(comment_data.get("created"))
    updated = _parse_timestamp(comment_data.get("updated"))

    return Comment(
        comment_id=str(comment_data.get("id", "")),
        issue_key=issue.get("key", ""),
        author=author.get("displayName") or author.get("emailAddress") or "",
        author_role=None,
        created=created,
        updated=updated,
        body=comment_data.get("body", "") or "",
        is_internal=False,
    )


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.utcnow()

    try:
        normalized = value.replace("+0000", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.utcnow()


@app.post("/approve")
async def approve_draft(request: Request):
    """
    Endpoint to approve a draft response.
    Can be called from Jira UI or Teams.
    """
    try:
        payload = await request.json()
        draft_id = payload.get("draft_id")
        approved_by = payload.get("approved_by")
        
        logger.info(f"Draft {draft_id} approved by {approved_by}")
        
        # TODO: Retrieve draft, post comment, update audit log
        
        return {"status": "approved", "draft_id": draft_id}
    except Exception as e:
        logger.error(f"Error approving draft: {e}")
        raise HTTPException(status_code=500, detail="Failed to approve draft")


@app.post("/reject")
async def reject_draft(request: Request):
    """Endpoint to reject a draft response"""
    try:
        payload = await request.json()
        draft_id = payload.get("draft_id")
        feedback = payload.get("feedback", "")
        
        logger.info(f"Draft {draft_id} rejected. Feedback: {feedback}")
        
        # TODO: Mark draft as rejected, store feedback
        
        return {"status": "rejected", "draft_id": draft_id}
    except Exception as e:
        logger.error(f"Error rejecting draft: {e}")
        raise HTTPException(status_code=500, detail="Failed to reject draft")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
