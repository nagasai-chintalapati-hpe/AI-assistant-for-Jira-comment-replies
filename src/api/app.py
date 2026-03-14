"""FastAPI application – Jira webhook listener + agent orchestration.

Architecture flow (matches high-level design):
  Jira Cloud
    → Webhook Listener   (POST /webhook/jira)
    → Event Filter       (type, status, keyword, idempotency)
    → Orchestrator       (_orchestrate)
        ├─ Classifier     (CommentClassifier)
        ├─ Context        (ContextCollector + Tooling Layer)
        ├─ LLM / Drafter  (ResponseDrafter)
        └─ Draft Store    (SQLiteDraftStore)
    → Approval Service   (POST /approve, POST /reject)
    → Action Executor    (post comment to Jira on approval)
    → Draft Review UI    (GET /ui, GET /ui/drafts/{id}, POST approve/reject)
    → RAG Pipeline       (POST /rag/ingest/*, GET /rag/search)
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
import hashlib
import hmac
import json as _json
import logging
import os
import time as _time
from collections import defaultdict
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
from src.storage.sqlite_store import SQLiteDraftStore, SQLiteIdempotencyStore
from src.integrations.log_lookup import LogLookupService
from src.integrations.testrail import TestRailClient
from src.integrations.jira import JiraClient
from src.integrations.git import GitClient
from src.integrations.s3_connector import S3ArtifactFetcher
from src.queue.broker import MessageBroker
from src.llm.client import get_llm_client
from src.utils.redactor import redact_with_stats
from src.config import settings

import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


# Webhook signature verification (HMAC-SHA256)

def _verify_signature(body: bytes, signature_header: Optional[str]) -> bool:
    """Verify the Jira webhook HMAC-SHA256 signature.

    Returns ``True`` when:
      * Signature validation is disabled (``VALIDATE_WEBHOOK_SIGNATURE=false``
        or ``JIRA_WEBHOOK_SECRET`` not set)  — safe default for local dev.
      * The computed HMAC matches the ``X-Hub-Signature`` header value.
    Returns ``False`` when validation is enabled but the signature is missing
    or doesn't match.
    """
    if not settings.webhook.validate_signature or not settings.webhook.secret:
        return True
    if not signature_header:
        logger.warning("Webhook received without X-Hub-Signature header — rejecting")
        return False
    try:
        scheme, provided = signature_header.split("=", 1)
        if scheme != "sha256":
            return False
    except ValueError:
        return False
    expected = hmac.new(
        settings.webhook.secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


# Rate limiter (per-IP, Redis-backed or in-memory)

class _RateLimiter:
    """Per-IP rate limiter — Redis-backed when REDIS_ENABLED=true, else in-memory.

    Redis mode uses a sorted-set sliding window (one key per client IP).
    Falls back to an in-process ``defaultdict`` when Redis is unavailable.
    """

    def __init__(self, rpm: int = 60) -> None:
        self._rpm = rpm
        self._counts: dict[str, list[float]] = defaultdict(list)
        self._redis = self._init_redis()

    def _init_redis(self):
        """Try to connect to Redis; return client or ``None`` on failure."""
        if not settings.redis.enabled:
            return None
        try:
            import redis  # type: ignore[import]

            url = settings.redis.url or (
                f"redis://:{settings.redis.password}@{settings.redis.host}"
                f":{settings.redis.port}/{settings.redis.db}"
                if settings.redis.password
                else f"redis://{settings.redis.host}:{settings.redis.port}/{settings.redis.db}"
            )
            client = redis.from_url(url, socket_connect_timeout=2)
            client.ping()
            logger.info(
                "Redis rate limiter connected (%s:%s)",
                settings.redis.host,
                settings.redis.port,
            )
            return client
        except ImportError:
            logger.warning(
                "redis-py not installed — using in-memory rate limiter "
                "(install redis for multi-process HA)"
            )
        except Exception as exc:
            logger.warning(
                "Redis unavailable (%s) — falling back to in-memory rate limiter", exc
            )
        return None

    def is_allowed(self, key: str) -> bool:
        if not settings.rate_limit.enabled:
            return True
        if self._redis is not None:
            return self._is_allowed_redis(key)
        return self._is_allowed_memory(key)

    def _is_allowed_redis(self, key: str) -> bool:
        """Sliding-window rate limit using a Redis sorted set."""
        try:
            import time as _time_module

            now = _time_module.time()
            window_key = f"rl:{key}"
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(window_key, 0, now - 60)
            pipe.zcard(window_key)
            pipe.zadd(window_key, {str(now): now})
            pipe.expire(window_key, 120)
            _, count, *_ = pipe.execute()
            return int(count) < self._rpm
        except Exception as exc:
            logger.warning(
                "Redis rate-limit check failed (%s) — allowing request", exc
            )
            return True

    def _is_allowed_memory(self, key: str) -> bool:
        now = _time.monotonic()
        window_start = now - 60.0
        self._counts[key] = [t for t in self._counts[key] if t > window_start]
        if len(self._counts[key]) >= self._rpm:
            return False
        self._counts[key].append(now)
        return True

# Module-level singletons

draft_store = SQLiteDraftStore(db_path=settings.app.db_path)
_idempotency_store = SQLiteIdempotencyStore(db_path=settings.app.db_path)

event_filter = EventFilter(idempotency_store=_idempotency_store)

# Rate limiter (per IP)
_rate_limiter = _RateLimiter(rpm=settings.rate_limit.max_requests_per_minute)

# LLM / Drafter — Copilot SDK (GitHub Copilot API) or local llama.cpp
_llm_client = get_llm_client()
classifier = CommentClassifier(llm_client=_llm_client)
drafter = ResponseDrafter(llm_client=_llm_client)

# Draft store — persistent SQLite

# Tooling layer — connectors (each degrades gracefully when unconfigured)
_log_lookup = LogLookupService()
_testrail_client = TestRailClient()

_jira_client: Optional[JiraClient] = None
try:
    _jira_client = JiraClient()
except Exception:
    _jira_client = None

_git_client: Optional[GitClient] = None
try:
    _git_client = GitClient()
    if not _git_client.enabled:
        _git_client = None
except Exception:
    _git_client = None

# S3 artifact fetcher (gracefully disabled when S3_BUCKET not configured)
_s3_fetcher = S3ArtifactFetcher()

# Message broker — RabbitMQ async queue (gracefully disabled by default)
_broker = MessageBroker()

# Approval service — notification channels (Teams + Email)
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

# RAG pipeline — lazy-initialised on first call
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


def _sync_queue_handler(event_dict: dict) -> None:
    """Synchronous queue consumer callback — called by the RabbitMQ daemon thread.

    Deserialises the raw event dict, runs it through the full agent pipeline,
    and logs the result.  Errors are propagated so the broker can nack the
    message (preventing infinite re-delivery).
    """
    import asyncio

    event = JiraWebhookEvent(**event_dict)
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_orchestrate(event))
        logger.info(
            "Queue event processed — draft_id=%s issue=%s",
            result.get("draft_id"),
            result.get("issue_key"),
        )
    finally:
        loop.close()


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Application lifespan — startup / shutdown."""
    channels = []
    if _teams.enabled:
        channels.append("Teams")
    if _email.enabled:
        channels.append("Email")
    logger.info(
        "Starting Jira Comment Assistant API (v0.6.0) — notifications: %s",
        ", ".join(channels) if channels else "none",
    )
    if _broker.enabled:
        _broker.start_consumer(_sync_queue_handler)
        logger.info(
            "RabbitMQ consumer started — queue=%s", settings.queue.queue_name
        )
    yield
    _broker.stop()
    logger.info("Jira Comment Assistant API stopped")


app = FastAPI(
    title="Jira Comment Assistant",
    description="AI assistant for responding to Jira defect comments",
    version="0.6.0",
    lifespan=lifespan,
)

# Draft Review UI — static assets and Jinja2 templates
_ui_dir = Path(__file__).parent
app.mount(
    "/static",
    StaticFiles(directory=_ui_dir / "static"),
    name="static",
)
_templates = Jinja2Templates(directory=str(_ui_dir / "templates"))


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.6.0",
        "drafts_in_store": draft_store.count(),
        "notifications": {
            "teams": _teams.enabled,
            "email": _email.enabled,
        },
        "integrations": {
            "git": _git_client is not None,
            "elk": _log_lookup.elk_enabled,
            "testrail": _testrail_client.enabled,
            "rag": _rag_engine is not None,
            "s3": _s3_fetcher.enabled,
            "queue": _broker.enabled,
            "redis": _rate_limiter._redis is not None,
        },
    }


@app.get("/metrics")
async def get_metrics():
    """Return aggregated draft quality and processing metrics.

    Response includes acceptance rate, average confidence, average human
    rating, hallucination flag count, and breakdown by classification type.
    """
    return draft_store.get_metrics()

# Webhook listener
@app.post("/webhook/jira")
async def jira_webhook(request: Request):
    """
    Receive a Jira webhook event and run it through the agent pipeline.

    1. Rate-limit check (per client IP).
    2. HMAC-SHA256 signature validation (``X-Hub-Signature`` header).
    3. Parse + validate payload.
    4. EventFilter — gates on issue type, status, trigger keywords, idempotency.
    5. Hand off to the Orchestrator.
    """
    # 1. Rate limiting
    client_ip = (request.client.host if request.client else "unknown")
    if not _rate_limiter.is_allowed(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded — try again later")

    # 2. Signature validation
    body = await request.body()
    sig_header = request.headers.get("X-Hub-Signature")
    if not _verify_signature(body, sig_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # 3. Parse
    try:
        payload = _json.loads(body)
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

    # Filter
    result = event_filter.evaluate(event)
    if not result.accepted:
        logger.info("Event filtered out: %s", result.reason)
        return {
            "status": "filtered",
            "reason": result.reason,
            "event_id": result.event_id,
        }

    # Orchestrate — via message queue (async) or synchronous fallback
    if _broker.enabled:
        published = _broker.publish(payload)
        if published:
            return JSONResponse(
                status_code=202,
                content={
                    "status": "queued",
                    "event_id": event.event_id,
                    "issue_key": event.issue_key,
                },
            )
        logger.warning(
            "Queue publish failed — falling back to synchronous processing"
        )
    return await _orchestrate(event)


# Orchestrator

async def _orchestrate(event: JiraWebhookEvent):
    """Orchestrator — runs the full agent pipeline for a comment event.

    Matches the Orchestrator/Workflow Engine in the architecture diagram:
      Comment → Classify → Context (Tooling Layer) → Draft (LLM) → Store
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

    # 2. Redact PII from comment body before sending to Copilot LLM
    _redaction = redact_with_stats(comment.body)
    if _redaction.redaction_count > 0:
        logger.info(
            "Redacted %d sensitive pattern(s) from comment %s",
            _redaction.redaction_count,
            comment.comment_id,
        )
        comment = comment.model_copy(update={"body": _redaction.text})

    # 3. Classify
    classification = classifier.classify(comment)
    logger.info(
        "Classified %s comment %s → %s (%.2f)",
        comment.issue_key,
        comment.comment_id,
        classification.comment_type.value,
        classification.confidence,
    )

    # 4. Context collection (deferred if Jira creds not configured)
    context = _collect_context_safe(comment.issue_key)

    # 5. Draft response
    draft = drafter.draft(comment, classification, context)
    logger.info("Generated draft %s for %s", draft.draft_id, comment.issue_key)

    # 6. Store (persistent SQLite)
    draft_store.save(draft, classification=classification.comment_type.value)

    # 7. Notify (Teams / Email — optional, fire-and-forget)
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
    """Collect context via the Tooling Layer; return a minimal stub on failure."""
    try:
        from src.agent.context_collector import ContextCollector

        collector = ContextCollector(
            jira_client=_jira_client,
            log_lookup=_log_lookup,
            testrail_client=_testrail_client,
            git_client=_git_client,
            s3_fetcher=_s3_fetcher if _s3_fetcher.enabled else None,
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

# Draft store — retrieval
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

# Approval service + action executor
@app.post("/approve")
async def approve_draft(request: Request):
    """
    Approve a draft — human-in-the-loop gate.

    1. Mark draft APPROVED in store.
    2. Action Executor: post comment to Jira (approved only).
    3. Mark draft POSTED.
    4. Notify via Approval Service (Teams / Email).
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

        # Action executor — post to Jira only on explicit approval
        if post_to_jira and _jira_client is not None:
            try:
                body = draft_data.get("body", "")
                # Write draft to Jira custom field for auditability (if configured)
                field_id = settings.jira.draft_field_id
                if field_id:
                    _jira_client.update_custom_field(issue_key, field_id, body)
                    logger.info(
                        "Draft %s stored in Jira field %s on %s",
                        draft_id, field_id, issue_key,
                    )
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


# RAG pipeline

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

    # Accept both 'source_title' and 'title' for flexibility
    title = payload.get("source_title") or payload.get("title")
    text = payload.get("text")
    if not title or not text:
        raise HTTPException(status_code=400, detail="'source_title' and 'text' are required")

    source_type = payload.get("source_type", "text")
    url = payload.get("source_url") or payload.get("url")
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


# Draft Review UI

@app.get("/ui")
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


@app.get("/ui/drafts/{draft_id}")
async def ui_review(request: Request, draft_id: str):
    """Draft review page — shows full draft with evidence panel and approve/reject actions."""
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


@app.post("/ui/drafts/{draft_id}/approve")
async def ui_approve(
    request: Request,
    draft_id: str,
    body: Optional[str] = Form(default=None),
):
    """Handle approve form submission — optionally update body, then approve and post to Jira."""
    draft_data = draft_store.get(draft_id)
    if draft_data is None:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Persist edited body if the human changed it
    effective_body = body.strip() if body and body.strip() else draft_data.get("body", "")
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


@app.post("/ui/drafts/{draft_id}/reject")
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


@app.post("/ui/drafts/{draft_id}/rate")
async def rate_draft(draft_id: str, request: Request):
    """Rate a draft response with a 1–5 star quality score.

    Request body: ``{"rating": <int 1-5>}``

    Ratings are persisted to SQLite and aggregated in ``GET /metrics``.
    """
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
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
