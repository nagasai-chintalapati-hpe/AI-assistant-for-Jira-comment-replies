"""FastAPI application – full pipeline + approval workflow.

Webhook → filter → classify → context → draft → store.
Approval endpoints for human-in-the-loop review.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
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
from src.integrations.notifications import (
    TeamsNotifier,
    EmailNotifier,
    NotificationService,
)
from src.storage.sqlite_store import SQLiteDraftStore
from src.integrations.log_lookup import LogLookupService
from src.integrations.testrail import TestRailClient
from src.integrations.jira import JiraClient
from src.config import settings

import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# singletons 
event_filter = EventFilter()

# Copilot SDK API key — optional; leave empty for keyword-only mode
_COPILOT_API_KEY: Optional[str] = os.getenv("COPILOT_API_KEY")
_COPILOT_MODEL: str = os.getenv("COPILOT_MODEL", "gpt-4")

classifier = CommentClassifier(api_key=_COPILOT_API_KEY, model=_COPILOT_MODEL)
drafter = ResponseDrafter(api_key=_COPILOT_API_KEY, model=_COPILOT_MODEL)

# Persistent SQLite draft store (replaces in-memory dict)
draft_store = SQLiteDraftStore(db_path=settings.app.db_path)

# Log Lookup + TestRail (optional — gracefully disabled when unconfigured)
_log_lookup = LogLookupService()
_testrail_client = TestRailClient()

# Jira client for posting approved drafts
_jira_client: Optional[JiraClient] = None
try:
    _jira_client = JiraClient()
except Exception:
    _jira_client = None

# Notification service (optional — disabled when env vars are empty)
_teams = TeamsNotifier(webhook_url=os.getenv("TEAMS_WEBHOOK_URL"))
_email = EmailNotifier(
    smtp_host=os.getenv("SMTP_HOST"),
    smtp_port=int(os.getenv("SMTP_PORT", "587")),
    smtp_username=os.getenv("SMTP_USERNAME"),
    smtp_password=os.getenv("SMTP_PASSWORD"),
    from_address=os.getenv("EMAIL_FROM"),
    to_addresses=[
        a.strip()
        for a in (os.getenv("EMAIL_TO") or "").split(",")
        if a.strip()
    ],
)
notifier = NotificationService(teams=_teams, email=_email)

# RAG engine + ingester (lazy — initialised on first RAG endpoint call)
_rag_engine = None
_rag_ingester = None


def _get_rag_engine():
    """Lazy-initialise the RAG engine singleton."""
    global _rag_engine
    if _rag_engine is None:
        from src.rag.engine import RAGEngine
        _rag_engine = RAGEngine()
    return _rag_engine


def _get_rag_ingester():
    """Lazy-initialise the document ingester singleton."""
    global _rag_ingester
    if _rag_ingester is None:
        from src.rag.ingest import DocumentIngester
        _rag_ingester = DocumentIngester(_get_rag_engine())
    return _rag_ingester


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Application lifespan — startup / shutdown."""
    channels = []
    if _teams.enabled:
        channels.append("Teams")
    if _email.enabled:
        channels.append("Email")
    logger.info(
        "Starting Jira Comment Assistant API (v0.5.0) — notifications: %s",
        ", ".join(channels) if channels else "none",
    )
    yield


app = FastAPI(
    title="Jira Comment Assistant",
    description="AI assistant for responding to Jira defect comments",
    version="0.5.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.5.0",
        "drafts_in_store": draft_store.count(),
        "notifications": {
            "teams": _teams.enabled,
            "email": _email.enabled,
        },
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

    # Parse
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

    # Filter
    result = event_filter.evaluate(event)
    if not result.accepted:
        logger.info("Event filtered out: %s", result.reason)
        return {
            "status": "filtered",
            "reason": result.reason,
            "event_id": result.event_id,
        }

    # Orchestrate
    return await handle_comment_event(event)


# Orchestration

async def handle_comment_event(event: JiraWebhookEvent):
    """
    Full pipeline:
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

    # 5. Store (persistent SQLite)
    draft_store.save(draft, classification=classification.comment_type.value)

    # 6. Notify (Teams / Email — optional, fire-and-forget)
    notifier.notify_draft_generated(
        draft_id=draft.draft_id,
        issue_key=comment.issue_key,
        classification=classification.comment_type.value,
        confidence=classification.confidence,
        body_preview=draft.body,
    )

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

        collector = ContextCollector(
            log_lookup=_log_lookup,
            testrail_client=_testrail_client,
        )
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
async def list_drafts(
    issue_key: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    """List all drafts, optionally filtered by issue_key and/or status."""
    drafts = draft_store.list_all(
        issue_key=issue_key, status=status, limit=limit, offset=offset,
    )
    total = draft_store.count(issue_key=issue_key, status=status)
    return {"count": len(drafts), "total": total, "drafts": drafts}

#  Approval endpoint
@app.post("/approve")
async def approve_draft(request: Request):
    """
    Approve a draft response.

    1. Mark draft as APPROVED in SQLite.
    2. Post the comment to Jira (if Jira client is configured).
    3. Mark draft as POSTED in SQLite.
    4. Send notification.
    """
    try:
        payload = await request.json()
        draft_id = payload.get("draft_id")
        approved_by = payload.get("approved_by")
        post_to_jira = payload.get("post_to_jira", True)

        # Verify draft exists
        draft_data = draft_store.get(draft_id)
        if draft_data is None:
            raise HTTPException(status_code=404, detail="Draft not found")

        # 1. Mark as approved
        updated = draft_store.update_status(
            draft_id, DraftStatus.APPROVED, approved_by=approved_by,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Draft not found")

        logger.info("Draft %s approved by %s", draft_id, approved_by)

        issue_key = draft_data.get("issue_key", "")
        jira_posted = False

        # 2. Post to Jira (action executor)
        if post_to_jira and _jira_client is not None:
            try:
                body = draft_data.get("body", "")
                _jira_client.add_comment(issue_key, body)
                draft_store.mark_posted(draft_id)
                jira_posted = True
                logger.info(
                    "Draft %s posted to Jira %s", draft_id, issue_key,
                )
            except Exception as exc:
                logger.error(
                    "Failed to post draft %s to Jira: %s", draft_id, exc,
                )

        # 3. Notify (Teams / Email)
        notifier.notify_draft_approved(
            draft_id=draft_id,
            issue_key=issue_key,
            approved_by=approved_by or "unknown",
        )

        return {
            "status": "approved",
            "draft_id": draft_id,
            "posted_to_jira": jira_posted,
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
        payload = await request.json()
        draft_id = payload.get("draft_id")
        feedback = payload.get("feedback", "")

        draft_data = draft_store.get(draft_id)
        if draft_data is None:
            raise HTTPException(status_code=404, detail="Draft not found")

        updated = draft_store.update_status(
            draft_id, DraftStatus.REJECTED, feedback=feedback,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Draft not found")

        logger.info("Draft %s rejected. Feedback: %s", draft_id, feedback)

        # Notify (Teams / Email)
        notifier.notify_draft_rejected(
            draft_id=draft_id,
            issue_key=draft_data.get("issue_key", ""),
            feedback=feedback,
        )

        return {"status": "rejected", "draft_id": draft_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error rejecting draft: %s", e)
        raise HTTPException(status_code=500, detail="Failed to reject draft")


# ── RAG endpoints ─────────────────────────────────────────────────────

@app.post("/rag/ingest/pdf")
async def rag_ingest_pdf(file: UploadFile = File(...)):
    """
    Upload and ingest a PDF into the RAG index.

    The file is saved to the configured PDF upload directory, parsed,
    chunked, and indexed into ChromaDB.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are accepted")

    upload_dir = Path(settings.rag.pdf_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / file.filename

    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)

        ingester = _get_rag_ingester()
        count = ingester.ingest_pdf(str(dest), source_title=file.filename)

        return {
            "status": "ingested",
            "filename": file.filename,
            "chunks_indexed": count,
        }
    except Exception as exc:
        logger.error("PDF ingestion failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")


@app.post("/rag/ingest/text")
async def rag_ingest_text(request: Request):
    """
    Ingest raw text into the RAG index.

    Payload: {"title": str, "text": str, "source_type": str, "url": str|null, "metadata": dict|null}
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    title = payload.get("title")
    text = payload.get("text")
    if not title or not text:
        raise HTTPException(status_code=400, detail="'title' and 'text' are required")

    source_type = payload.get("source_type", "text")
    url = payload.get("url")
    metadata = payload.get("metadata")

    try:
        ingester = _get_rag_ingester()
        count = ingester.ingest_text(
            text=text,
            source_title=title,
            source_type=source_type,
            source_url=url,
            metadata=metadata,
        )
        return {"status": "ingested", "title": title, "chunks_indexed": count}
    except Exception as exc:
        logger.error("Text ingestion failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")


@app.post("/rag/ingest/confluence")
async def rag_ingest_confluence(request: Request):
    """
    Ingest one or more Confluence pages into the RAG index.

    Payload: {"page_ids": [str]} or {"space_key": str, "label": str|null}
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    page_ids = payload.get("page_ids", [])
    space_key = payload.get("space_key")
    label = payload.get("label")

    if not page_ids and not space_key:
        raise HTTPException(
            status_code=400,
            detail="Provide 'page_ids' or 'space_key' to ingest",
        )

    from src.integrations.confluence import ConfluenceClient
    conf_client = ConfluenceClient()
    if not conf_client.enabled:
        raise HTTPException(
            status_code=503,
            detail="Confluence is not configured (check env vars)",
        )

    ingester = _get_rag_ingester()
    total_chunks = 0
    pages_ingested = 0

    # If space_key given, discover pages first
    if space_key and not page_ids:
        pages = conf_client.search_pages(space_key=space_key, label=label)
        page_ids = [p["id"] for p in pages if p.get("id")]

    for pid in page_ids:
        try:
            count = ingester.ingest_confluence_page(pid, confluence_client=conf_client)
            total_chunks += count
            if count > 0:
                pages_ingested += 1
        except Exception as exc:
            logger.warning("Failed to ingest Confluence page %s: %s", pid, exc)

    return {
        "status": "ingested",
        "pages_ingested": pages_ingested,
        "total_chunks_indexed": total_chunks,
    }


@app.get("/rag/search")
async def rag_search(q: str, top_k: int = 5, source_type: Optional[str] = None):
    """
    Search the RAG index for relevant document chunks.

    Query params: ?q=search+text&top_k=5&source_type=confluence
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

    engine = _get_rag_engine()
    result = engine.query(text=q, top_k=top_k, source_type=source_type or None)

    return {
        "query": result.query,
        "total_chunks_searched": result.total_chunks_searched,
        "retrieval_duration_ms": result.retrieval_duration_ms,
        "results": [
            {
                "chunk_id": s.chunk_id,
                "source_type": s.source_type,
                "source_title": s.source_title,
                "source_url": s.source_url,
                "content": s.content,
                "relevance_score": s.relevance_score,
            }
            for s in result.snippets
        ],
    }


@app.get("/rag/stats")
async def rag_stats():
    """Return RAG collection statistics."""
    engine = _get_rag_engine()
    return engine.stats()


@app.delete("/rag/document/{source_title}")
async def rag_delete_document(source_title: str):
    """Remove all chunks for a given source document."""
    engine = _get_rag_engine()
    deleted = engine.delete_by_source(source_title)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="No chunks found for this source")
    return {"status": "deleted", "source_title": source_title, "chunks_deleted": deleted}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
