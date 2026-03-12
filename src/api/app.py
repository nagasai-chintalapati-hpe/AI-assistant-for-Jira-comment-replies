"""FastAPI application – full pipeline + approval workflow.

Pipeline: Webhook → Filter → Classify → Context → Draft → Store
Approval: Human reviews draft → Approve (posts to Jira) or Reject
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

import src.config  # noqa: F401 — triggers dotenv loading

from src.agent.classifier import CommentClassifier
from src.agent.drafter import ResponseDrafter
from src.integrations.notifications import (
    TeamsNotifier,
    EmailNotifier,
    NotificationService,
)
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

# In-memory draft store
draft_store: dict[str, dict] = {}

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


# App lifecycle
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


# Health check
@app.get("/health")
async def health_check():
    """Return service status and version."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.5.0",
        "drafts_in_store": len(draft_store),
        "notifications": {
            "teams": _teams.enabled,
            "email": _email.enabled,
        },
    }


# Webhook endpoint
@app.post("/webhook/jira")
async def jira_webhook(request: Request):
    """
    Receive a Jira webhook event and run the full pipeline:

    1. Validate signature (if WEBHOOK_SECRET is set).
    2. Parse payload into JiraWebhookEvent.
    3. Run through EventFilter gates.
    4. Orchestrate: classify → context → draft → store.
    """
    body = await request.body()
    _verify_webhook_signature(request, body)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

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

    result = event_filter.evaluate(event)
    if not result.accepted:
        logger.info("Event filtered out: %s", result.reason)
        return {
            "status": "filtered",
            "reason": result.reason,
            "event_id": result.event_id,
        }

    return await _handle_comment_event(event)


# Pipeline 
async def _handle_comment_event(event: JiraWebhookEvent) -> dict:
    """
    Full pipeline:
      Comment → Classify → Context → Draft → Store
    """
    assert event.comment is not None
    assert event.issue is not None

    # 1. Build Comment model
    comment = _build_comment(event)

    # 2. Retrieve: Jira issue + attachments + last comments + Jenkins console logs
    loop = asyncio.get_running_loop()
    context = await loop.run_in_executor(None, _collect_context_safe, comment.issue_key)
    logger.info(
        "Collected context for %s (jenkins_links=%d, attachments=%d)",
        comment.issue_key,
        len(context.jenkins_links or []),
        len((context.issue_context.attached_files or []) if context.issue_context else []),
    )

    # 3. Classify (with full context for richer evidence-based classification)
    classification = await classifier.classify(comment, context=context)
    logger.info(
        "Classified %s comment %s → %s (%.2f)",
        comment.issue_key,
        comment.comment_id,
        classification.comment_type.value,
        classification.confidence,
    )

    # 4. Draft response + evidence list
    draft = await drafter.draft(comment, classification, context)
    logger.info("Generated draft %s for %s", draft.draft_id, comment.issue_key)

    # 5. Store
    draft_data = draft.model_dump(mode="json")
    draft_store[draft.draft_id] = draft_data

    # 6. Notify reviewers (best-effort)
    try:
        from src.integrations.notifications import notify_draft_event

        await loop.run_in_executor(
            None,
            lambda: notify_draft_event(draft_data, event_name="generated"),
        )
    except Exception as exc:
        logger.warning("Notification failed (non-fatal): %s", exc)

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


# Draft retrieval
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


# Approval / rejection
@app.post("/approve/{draft_id}")
async def approve_draft_short(draft_id: str, request: Request):
    """Approve a draft by path — POST /approve/draft_abc123."""
    return await _do_approve(draft_id, request)


@app.post("/approve")
async def approve_draft(request: Request):
    """Approve a draft (JSON body with draft_id)."""
    payload = await request.json()
    draft_id = payload.get("draft_id")
    return await _do_approve(draft_id, request, approved_by=payload.get("approved_by"))


async def _do_approve(draft_id: str, request: Request, approved_by: str | None = None):
    """Shared approve logic."""
    try:
        _verify_approval_auth(request)

        if not draft_id or draft_id not in draft_store:
            raise HTTPException(status_code=404, detail="Draft not found")

        if approved_by is None:
            try:
                payload = await request.json()
                approved_by = payload.get("approved_by")
            except Exception:
                approved_by = "api"

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

        # Notify on approval (best-effort)
        try:
            loop = asyncio.get_running_loop()
            from src.integrations.notifications import notify_draft_event

            await loop.run_in_executor(
                None,
                lambda: notify_draft_event(
                    draft,
                    event_name="approved",
                    actor=approved_by,
                ),
            )
        except Exception as exc:
            logger.warning("Approval notification failed (non-fatal): %s", exc)

        return {"status": "approved", "draft_id": draft_id, **post_result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error approving draft: %s", e)
        raise HTTPException(status_code=500, detail="Failed to approve draft")


@app.post("/reject/{draft_id}")
async def reject_draft_short(draft_id: str, request: Request):
    """Reject a draft by path — POST /reject/draft_abc123."""
    return await _do_reject(draft_id, request)


@app.post("/reject")
async def reject_draft(request: Request):
    """Reject a draft (JSON body with draft_id)."""
    payload = await request.json()
    draft_id = payload.get("draft_id")
    return await _do_reject(draft_id, request, feedback=payload.get("feedback", ""))


async def _do_reject(draft_id: str, request: Request, feedback: str = ""):
    """Shared reject logic."""
    try:
        _verify_approval_auth(request)

        if not draft_id or draft_id not in draft_store:
            raise HTTPException(status_code=404, detail="Draft not found")

        if not feedback:
            try:
                payload = await request.json()
                feedback = payload.get("feedback", "")
            except Exception:
                pass

        draft = draft_store.get(draft_id)
        assert draft is not None
        draft["status"] = DraftStatus.REJECTED.value
        draft["feedback"] = feedback
        draft_store[draft_id] = draft

        logger.info("Draft %s rejected. Feedback: %s", draft_id, feedback)

        # Notify on rejection (best-effort)
        try:
            loop = asyncio.get_running_loop()
            from src.integrations.notifications import notify_draft_event

            await loop.run_in_executor(
                None,
                lambda: notify_draft_event(
                    draft,
                    event_name="rejected",
                    feedback=feedback,
                ),
            )
        except Exception as exc:
            logger.warning("Rejection notification failed (non-fatal): %s", exc)

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
