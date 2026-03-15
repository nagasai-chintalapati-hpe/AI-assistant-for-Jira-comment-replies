"""Webhook listener — POST /webhook/jira.

Receives Jira webhook payloads, validates them, applies the EventFilter,
then hands off to the Orchestrator (directly or via the message queue).
"""

from __future__ import annotations

import json as _json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.api.deps import event_filter, _broker, _rate_limiter
from src.api.security import _verify_signature
from src.api.orchestrator import _orchestrate
from src.models.webhook import JiraWebhookEvent

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/webhook/jira")
async def jira_webhook(request: Request):
    """Receive a Jira webhook event and run it through the agent pipeline.

    1. Rate-limit check (per client IP).
    2. HMAC-SHA256 signature validation (``X-Hub-Signature`` header).
    3. Parse + validate payload.
    4. EventFilter — gates on issue type, status, trigger keywords, idempotency.
    5. Hand off to the Orchestrator (queue or sync).
    """
    # 1. Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limiter.is_allowed(client_ip):
        raise HTTPException(
            status_code=429, detail="Rate limit exceeded — try again later"
        )

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

    # 4. EventFilter
    result = event_filter.evaluate(event)
    if not result.accepted:
        logger.info("Event filtered out: %s", result.reason)
        return {
            "status": "filtered",
            "reason": result.reason,
            "event_id": result.event_id,
        }

    # 5. Orchestrate — via message queue (async) or synchronous fallback
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
