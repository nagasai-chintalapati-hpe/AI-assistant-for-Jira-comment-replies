"""
Webhook event filtering and validation.

Applies the MVP v1 gate rules described in the architecture doc:
  • Issue type must be Bug / Defect
  • Issue status in an allowed set (In Progress, Ready for QA, Reopened, Open)
  • Comment author should belong to the developer group OR trigger
    heuristic keywords ("cannot repro", "fixed in", "need logs", etc.)
  • Idempotency: duplicate event IDs are rejected
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.models.webhook import JiraWebhookEvent

logger = logging.getLogger(__name__)

# Configurable allow-lists

ALLOWED_ISSUE_TYPES: set[str] = {"Bug", "Defect", "bug", "defect"}

ALLOWED_STATUSES: set[str] = {
    "Open",
    "In Progress",
    "Ready for QA",
    "Reopened",
    "To Do",
    "In Review",
}

HANDLED_EVENTS: set[str] = {"comment_created", "comment_updated", "jira:issue_updated"}

# Keywords that strongly suggest the comment is worth processing even when
# we cannot determine the author's role from Jira groups.
TRIGGER_KEYWORDS: list[str] = [
    "cannot reproduce",
    "can't reproduce",
    "cannot repro",
    "can't repro",
    "need logs",
    "need more info",
    "as designed",
    "by design",
    "expected behavior",
    "expected behaviour",
    "already fixed",
    "fixed in",
    "fix ready",
    "fix deployed",
    "please validate",
    "please verify",
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

    def __init__(self) -> None:
        self._seen_event_ids: set[str] = set()

    # Public API

    def evaluate(self, event: JiraWebhookEvent) -> FilterResult:
        """Run all filter rules against *event* and return a FilterResult."""

        # 1. Event type check
        if event.webhookEvent not in HANDLED_EVENTS:
            return FilterResult(
                accepted=False,
                reason=f"Unhandled event type: {event.webhookEvent}",
            )

        # 2. Idempotency
        eid = event.event_id
        if eid in self._seen_event_ids:
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

        # 6. Comment author / keyword heuristic
        if not self._comment_is_relevant(event):
            return FilterResult(
                accepted=False,
                reason="Comment does not match developer-group or keyword heuristics",
                event_id=eid,
            )

        # All gates passed – mark as seen
        self._seen_event_ids.add(eid)
        logger.info("Event %s accepted for processing", eid)
        return FilterResult(accepted=True, reason="accepted", event_id=eid)

    # Private helpers

    @staticmethod
    def _comment_is_relevant(event: JiraWebhookEvent) -> bool:
        """
        Return True when the comment should be processed.

        For MVP v1 we use keyword matching against the comment body as
        a proxy for "comment author belongs to the developer group".
        """
        if event.comment is None:
            return False
        body_lower = event.comment.body.lower()
        return any(kw in body_lower for kw in TRIGGER_KEYWORDS)

    def reset(self) -> None:
        """Clear the idempotency set (useful in tests)."""
        self._seen_event_ids.clear()
