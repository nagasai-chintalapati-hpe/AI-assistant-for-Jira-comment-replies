"""Webhook event filtering and validation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.models.webhook import JiraWebhookEvent
import src.config as _cfg

logger = logging.getLogger(__name__)

# Configurable allow-lists

ALLOWED_ISSUE_TYPES: set[str] = {
    "Bug", "Defect", "Story", "Task", "Sub-task", "Epic",
    "Improvement", "New Feature", "Support", "Incident",
    "Problem", "Change", "Service Request",
    # lowercase variants for case-insensitive matching
    "bug", "defect", "story", "task", "sub-task", "epic",
    "improvement", "new feature", "support", "incident",
    "problem", "change", "service request",
}

ALLOWED_STATUSES: set[str] = {
    "Open",
    "In Progress",
    "Ready for QA",
    "Reopened",
    "To Do",
    "In Review",
    "Cannot_reproduce",
    "Cannot Reproduce",
    "In Testing",
    "Under Review",
    "Pending",
    "Blocked",
    "Closed",
    "Resolved",
    "Done",
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
    "duplicate",
    "duplicate of",
    "same as",
    "known issue",
    "blocked by",
    "waiting for",
    "depends on",
    "blocked on",
    "configuration issue",
    "config issue",
    "not a bug",
    "misconfigured",
    "setup issue",
]


@dataclass
class FilterResult:
    """Outcome of running the event through the filter pipeline."""
    accepted: bool
    reason: str
    event_id: Optional[str] = None


class EventFilter:
    """Stateful filter that gates incoming Jira webhook events."""

    def __init__(self, idempotency_store=None) -> None:
        self._seen_event_ids: set[str] = set()  # In-memory fast-path cache
        self._store = idempotency_store           # Optional persistent back-end

    # Public API

    def evaluate(self, event: JiraWebhookEvent) -> FilterResult:
        """Run all filter rules against *event* and return a FilterResult."""

        # 1. Event type check
        if event.webhookEvent not in HANDLED_EVENTS:
            return FilterResult(
                accepted=False,
                reason=f"Unhandled event type: {event.webhookEvent}",
            )

        # 2. Idempotency (in-memory cache + optional persistent store)
        eid = event.event_id
        if self._is_seen(eid):
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
                reason=f"Issue type '{issue_type}' is not in the allowed set",
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

        # 6. QA team gate — for Bug/Defect issues, only process if reporter is QA
        if self._is_defect_type(issue_type) and not self._reporter_is_qa(event):
            return FilterResult(
                accepted=False,
                reason=(
                    f"Issue reporter '{event.reporter_display_name or event.reporter_account_id or 'unknown'}' "
                    f"is not a recognised QA team member — skipping defect"
                ),
                event_id=eid,
            )

        # 7. Comment author / keyword heuristic (informational only)
        # All comments on Bug/Defect issues are processed. Keywords are used
        # downstream by the classifier to improve draft quality, not to gate here.
        if not self._comment_is_relevant(event):
            logger.debug(
                "Comment %s has no trigger keywords — processing anyway (issue type: %s)",
                eid, issue_type,
            )

        # All gates passed – mark as seen (memory + optional persistent store)
        self._mark_seen(eid)
        logger.info("Event %s accepted for processing", eid)
        return FilterResult(accepted=True, reason="accepted", event_id=eid)

    # Private helpers

    @staticmethod
    def _is_defect_type(issue_type: str | None) -> bool:
        """Return True when the issue type is a bug / defect."""
        if not issue_type:
            return False
        return issue_type.lower() in ("bug", "defect")

    @staticmethod
    def _reporter_is_qa(event: JiraWebhookEvent) -> bool:
        """Check whether the issue reporter belongs to the QA team.

        Uses the QA team config from settings: account IDs, display-name
        substrings, and (optionally) Jira group membership.
        When the QA filter is disabled via config, everyone passes.
        """
        qa_cfg = _cfg.settings.qa_team
        if not qa_cfg.enabled:
            return True  # filter disabled — allow all reporters

        # If no QA identifiers configured at all, allow everything
        # (avoids accidentally blocking all issues on first deploy)
        if not qa_cfg.account_ids and not qa_cfg.display_names and not qa_cfg.jira_groups:
            logger.debug("QA team filter enabled but no identifiers configured — allowing all reporters")
            return True

        # Check by account ID
        reporter_id = event.reporter_account_id
        if reporter_id and reporter_id in qa_cfg.account_id_set:
            return True

        # Check by display name substring
        reporter_name = (event.reporter_display_name or "").lower()
        if reporter_name:
            for pattern in qa_cfg.display_name_patterns:
                if pattern in reporter_name:
                    return True

        # Check by email domain or full email (treat as display-name match)
        reporter_email = (event.reporter_email or "").lower()
        if reporter_email:
            for pattern in qa_cfg.display_name_patterns:
                if pattern in reporter_email:
                    return True

        return False

    @staticmethod
    def _comment_is_relevant(event: JiraWebhookEvent) -> bool:
        """Check if comment contains trigger keywords."""
        if event.comment is None:
            return False
        body_lower = event.comment.body.lower()
        return any(kw in body_lower for kw in TRIGGER_KEYWORDS)

    # Idempotency helpers

    def _is_seen(self, eid: str) -> bool:
        """Return True if *eid* has already been processed."""
        if eid in self._seen_event_ids:
            return True
        if self._store is not None:
            return self._store.is_seen(eid)
        return False

    def _mark_seen(self, eid: str) -> None:
        """Record *eid* as processed in both the in-memory cache and the store."""
        self._seen_event_ids.add(eid)
        if self._store is not None:
            self._store.mark_seen(eid)

    def reset(self) -> None:
        """Clear the idempotency cache (in-memory only; useful in tests)."""
        self._seen_event_ids.clear()
