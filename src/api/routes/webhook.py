"""Jira webhook listener."""

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


def _extract_adf_text(adf: dict) -> str:
    """Extract plain text from Atlassian Document Format (ADF)."""
    if not isinstance(adf, dict):
        return str(adf)

    parts: list[str] = []

    # Text leaf node
    if adf.get("type") == "text":
        parts.append(adf.get("text", ""))

    # Recurse into content array
    for child in adf.get("content", []):
        parts.append(_extract_adf_text(child))

    text = "".join(parts)

    # Add newlines for block-level elements
    if adf.get("type") in ("paragraph", "heading", "bulletList", "orderedList", "listItem", "codeBlock"):
        text = text.strip() + "\n"

    return text


@router.post("/webhook/jira")
async def jira_webhook(request: Request):
    """Receive a Jira webhook event and process it."""
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limiter.is_allowed(client_ip):
        raise HTTPException(
            status_code=429, detail="Rate limit exceeded — try again later"
        )

    body = await request.body()
    sig_header = request.headers.get("X-Hub-Signature")
    if not _verify_signature(body, sig_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Parse payload
    try:
        payload = _json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Jira Cloud may send comment body as ADF (dict) instead of plain text —
    # normalise it so the Pydantic model always gets a string.
    _raw_comment = payload.get("comment")
    if isinstance(_raw_comment, dict):
        raw_body = _raw_comment.get("body")
        if isinstance(raw_body, dict):
            # ADF → extract plain text from content nodes
            payload["comment"]["body"] = _extract_adf_text(raw_body)

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
