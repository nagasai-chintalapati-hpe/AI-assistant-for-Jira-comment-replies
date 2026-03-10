"""Webhook event filtering and validation."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from src.models.webhook import JiraWebhookEvent

logger = logging.getLogger(__name__)

# Bot-loop protection
_BOT_USERNAME: str = os.getenv("JIRA_USERNAME", "")
BOT_COMMENT_MARKER = "\u200B\u200C\u200B\u200C\u200B"

# Configurable allow-lists

ALLOWED_ISSUE_TYPES: set[str] = {"Bug", "Defect", "bug", "defect"}

ALLOWED_STATUSES: set[str] = {
    "Open",
    "In Progress",
    "In Test",
    "Ready for QA",
    "Reopened",
    "To Do",
    "In Review",
    "New",
    "Cannot Reproduce",
    "Cannot_reproduce",
    "Resolved",
    "Closed",
}

HANDLED_EVENTS: set[str] = {"comment_created", "comment_updated", "jira:issue_updated"}

# Keywords that strongly suggest the comment is worth processing even when
# we cannot determine the author's role from Jira groups.
TRIGGER_KEYWORDS: list[str] = [
    "cannot reproduce",
    "can't reproduce",
    "cannot repro",
    "can't repro",
    "unable to reproduce",
    "unable to repro",
    "not reproducible",
    "works on my machine",
    "works for me",
    "failed to reproduce",
    "need logs",
    "need more info",
    "need more details",
    "missing context",
    "provide logs",
    "as designed",
    "by design",
    "expected behavior",
    "expected behaviour",
    "working as intended",
    "not a bug",
    "already fixed",
    "fixed in",
    "fix ready",
    "fix deployed",
    "please validate",
    "please verify",
    "merged",
    "deployed",
    "released",
]


@dataclass
class FilterResult:
    """Outcome of running the event through the filter pipeline."""

    accepted: bool
    reason: str
    event_id: Optional[str] = None


class EventFilter:
    """
    Stateful filter that gates incoming Jira webhook events.

    Tracks seen event IDs for idempotency within the lifetime of the
    process (swap with Redis / DB for production).
    """

    def __init__(self, event_store: Optional[object] = None) -> None:
        self._seen_event_ids: set[str] = set()
        self._event_store = event_store

    # Public API

    def evaluate(self, event: JiraWebhookEvent) -> FilterResult:
        """Run all filter rules against *event* and return a FilterResult."""

        # 0. Bot-loop protection – ignore comments posted by the bot
        if event.comment is not None:
            body = getattr(event.comment, "body", "") or ""
            # Primary check: bot marker in the comment body
            if BOT_COMMENT_MARKER in body:
                return FilterResult(
                    accepted=False,
                    reason="Ignoring comment containing bot marker",
                )

        # 1. Event type check
        if event.webhookEvent not in HANDLED_EVENTS:
            return FilterResult(
                accepted=False,
                reason=f"Unhandled event type: {event.webhookEvent}",
            )

        # 2. Idempotency
        eid = event.event_id
        if self._has_seen_event(eid):
            return FilterResult(
                accepted=False,
                reason=f"Duplicate event (already processed): {eid}",
                event_id=eid,
            )

        # 3. Must have issue & comment
        if event.issue is None:
            return FilterResult(accepted=False, reason="Payload missing issue data")
        if event.comment is None:
            return FilterResult(accepted=False, reason="Payload missing comment data")

        # 4. Issue type gate
        issue_type = event.issue_type_name
        if issue_type and issue_type not in ALLOWED_ISSUE_TYPES:
            return FilterResult(
                accepted=False,
                reason=f"Issue type '{issue_type}' is not Bug/Defect",
                event_id=eid,
            )

        # 5. Status gate
        status = event.issue_status_name
        if status and status not in ALLOWED_STATUSES:
            return FilterResult(
                accepted=False,
                reason=f"Issue status '{status}' is not in the allowed set",
                event_id=eid,
            )

        # 6. Comment must have a non-empty body
        if not event.comment.body or not event.comment.body.strip():
            return FilterResult(
                accepted=False,
                reason="Comment body is empty",
                event_id=eid,
            )

        # All gates passed – mark as seen
        self._mark_event_seen(eid)
        logger.info("Event %s accepted for processing", eid)
        return FilterResult(accepted=True, reason="accepted", event_id=eid)

    # Private helpers

    @staticmethod
    def _comment_is_relevant(event: JiraWebhookEvent) -> bool:
        """
        Return True when the comment should be processed.

        NOTE: This is no longer used in the main evaluate() path.
        The classifier handles bucket assignment — the filter only
        gates on issue type, status, dedup, and non-empty body.
        Kept for backward compatibility / testing.
        """
        if event.comment is None:
            return False
        body_lower = event.comment.body.lower()
        return any(kw in body_lower for kw in TRIGGER_KEYWORDS)

    def reset(self) -> None:
        """Clear the idempotency set (useful in tests)."""
        self._seen_event_ids.clear()
        if self._event_store and hasattr(self._event_store, "clear_processed_events"):
            self._event_store.clear_processed_events()

    def _has_seen_event(self, event_id: str) -> bool:
        if event_id in self._seen_event_ids:
            return True
        if self._event_store and hasattr(self._event_store, "is_event_processed"):
            return bool(self._event_store.is_event_processed(event_id))
        return False

    def _mark_event_seen(self, event_id: str) -> None:
        self._seen_event_ids.add(event_id)
        if self._event_store and hasattr(self._event_store, "mark_event_processed"):
            self._event_store.mark_event_processed(event_id)
