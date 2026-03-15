"""FastAPI application - Jira webhook listener + agent orchestration.

Architecture flow:
  Jira Cloud -> Webhook Listener -> Event Filter -> Orchestrator
    (Classify -> Collect Context -> Draft -> Store -> Notify)
  -> Approval Service -> Action Executor -> Jira

Module layout
-------------
security.py      - HMAC verification + RateLimiter class
deps.py          - all singleton instances
orchestrator.py  - _orchestrate pipeline + context collector
routes/webhook.py  - POST /webhook/jira
routes/drafts.py   - GET /drafts, POST /reject
routes/rag.py      - all /rag/* endpoints
routes/health.py   - /health, /health/deep, /metrics, /metrics/prometheus
routes/admin.py    - /admin/drafts/purge-stale
routes/ui.py       - /ui/* review pages

NOTE: POST /approve lives here (not in routes/drafts.py) because the test
suite patches src.api.app._jira_client via unittest.mock - patching works by
replacing the name binding in the module where the handler is defined.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from src.config import settings
from src.models.draft import DraftStatus

# Singletons - defined in deps.py; imported here so that:
#   (a) tests can do  ``from src.api.app import app, draft_store, ...``
#   (b) _jira_client lives in THIS module's __dict__ for mock-patching
from src.api.deps import (
    draft_store,
    _idempotency_store,
    event_filter,
    _jira_client,
    notifier,
    _broker,
    _teams,
    _email,
)
from src.api.orchestrator import _sync_queue_handler
from src.api.routes import webhook as _webhook_routes
from src.api.routes import drafts as _drafts_routes
from src.api.routes import rag as _rag_routes
from src.api.routes import health as _health_routes
from src.api.routes import admin as _admin_routes
from src.api.routes import ui as _ui_routes

logger = logging.getLogger(__name__)


# Application lifespan


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Application lifespan - startup / shutdown."""
    channels = []
    if _teams.enabled:
        channels.append("Teams")
    if _email.enabled:
        channels.append("Email")
    logger.info(
        "Starting Jira Comment Assistant API (v0.6.0) - notifications: %s",
        ", ".join(channels) if channels else "none",
    )
    if _broker.enabled:
        _broker.start_consumer(_sync_queue_handler)
        logger.info(
            "RabbitMQ consumer started - queue=%s", settings.queue.queue_name
        )
    yield
    _broker.stop()
    logger.info("Jira Comment Assistant API stopped")


# FastAPI application instance

app = FastAPI(
    title="Jira Comment Assistant",
    description="AI assistant for responding to Jira defect comments",
    version="0.6.0",
    lifespan=lifespan,
)

# Static assets for the Draft Review UI
_ui_dir = Path(__file__).parent
app.mount(
    "/static",
    StaticFiles(directory=_ui_dir / "static"),
    name="static",
)

# Route modules
app.include_router(_webhook_routes.router)
app.include_router(_drafts_routes.router)
app.include_router(_rag_routes.router)
app.include_router(_health_routes.router)
app.include_router(_admin_routes.router)
app.include_router(_ui_routes.router)


# Approval action executor
# NOTE: This handler is intentionally defined here (not in routes/drafts.py).
# Tests patch ``src.api.app._jira_client`` and Python mock patching works by
# replacing the name binding in THIS module's __dict__. Moving the handler
# to another module would silently break those patches.


@app.post("/approve")
async def approve_draft(request: Request):
    """Approve a draft - human-in-the-loop gate.

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

        draft_data = draft_store.get(draft_id)
        if draft_data is None:
            raise HTTPException(status_code=404, detail="Draft not found")

        updated = draft_store.update_status(
            draft_id, DraftStatus.APPROVED, approved_by=approved_by
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Draft not found")

        logger.info("Draft %s approved by %s", draft_id, approved_by)

        issue_key = draft_data.get("issue_key", "")
        jira_posted = False

        # Action executor - post to Jira only on explicit approval
        if post_to_jira and _jira_client is not None:
            try:
                body = draft_data.get("body", "")
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
                logger.info("Draft %s posted to Jira %s", draft_id, issue_key)
            except Exception as exc:
                logger.error("Failed to post draft %s to Jira: %s", draft_id, exc)

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
    except Exception as exc:
        logger.error("Error approving draft: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to approve draft")
