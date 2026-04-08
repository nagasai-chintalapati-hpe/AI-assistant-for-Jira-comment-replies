"""FastAPI application — Jira webhook listener + agent orchestration."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from src.api.deps import (
    _broker,
    _email,
    _jira_client,
    _teams,
    draft_store,
    notifier,
)
from src.api.orchestrator import _sync_queue_handler
from src.api.routes import admin as _admin_routes
from src.api.routes import dashboard as _dashboard_routes
from src.api.routes import drafts as _drafts_routes
from src.api.routes import health as _health_routes
from src.api.routes import rag as _rag_routes
from src.api.routes import ui as _ui_routes
from src.api.routes import webhook as _webhook_routes
from src.config import settings
from src.models.draft import DraftStatus

logger = logging.getLogger(__name__)


# Application lifespan
@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Startup and shutdown lifecycle hooks."""
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


# App instance

app = FastAPI(
    title="Jira Comment Assistant",
    description="AI assistant for responding to Jira defect comments",
    version="0.6.0",
    lifespan=lifespan,
)

# Static assets (CSS, images)
_ui_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=_ui_dir / "static"), name="static")

# Route modules
app.include_router(_webhook_routes.router)
app.include_router(_drafts_routes.router)
app.include_router(_rag_routes.router)
app.include_router(_health_routes.router)
app.include_router(_admin_routes.router)
app.include_router(_ui_routes.router)
app.include_router(_dashboard_routes.router)


@app.post("/approve")
async def approve_draft(request: Request):
    """Approve a draft and optionally post it to Jira."""
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

        # Post to Jira only on explicit approval
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
