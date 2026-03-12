"""Webhook event models for Jira incoming payloads"""

from typing import Any, Optional

from pydantic import BaseModel


class WebhookUser(BaseModel):
    """Jira user from webhook payload"""

    accountId: Optional[str] = None
    displayName: Optional[str] = None
    emailAddress: Optional[str] = None
    active: Optional[bool] = True


class WebhookComment(BaseModel):
    """Comment section of a Jira webhook event"""

    id: str
    body: str
    author: WebhookUser
    created: str
    updated: str


class WebhookIssueFields(BaseModel):
    """Subset of issue fields from webhook payload"""

    summary: Optional[str] = None
    issuetype: Optional[dict[str, Any]] = None
    status: Optional[dict[str, Any]] = None
    priority: Optional[dict[str, Any]] = None
    labels: Optional[list[str]] = None
    environment: Optional[str] = None


class WebhookIssue(BaseModel):
    """Issue section of a Jira webhook event"""

    id: str
    key: str
    fields: WebhookIssueFields


class JiraWebhookEvent(BaseModel):
    """
    Top-level Jira webhook event payload.

    Jira sends different shapes depending on the event type.
    We normalise the pieces we care about.
    """

    webhookEvent: str
    timestamp: Optional[int] = None
    issue: Optional[WebhookIssue] = None
    comment: Optional[WebhookComment] = None

    # Derived helpers

    @property
    def event_id(self) -> str:
        """Deterministic key for idempotency checks."""
        parts = [
            self.webhookEvent,
            self.issue.key if self.issue else "no-issue",
            self.comment.id if self.comment else "no-comment",
            str(self.timestamp or 0),
        ]
        return ":".join(parts)

    @property
    def issue_key(self) -> Optional[str]:
        return self.issue.key if self.issue else None

    @property
    def issue_type_name(self) -> Optional[str]:
        if self.issue and self.issue.fields.issuetype:
            return self.issue.fields.issuetype.get("name")
        return None

    @property
    def issue_status_name(self) -> Optional[str]:
        if self.issue and self.issue.fields.status:
            return self.issue.fields.status.get("name")
        return None
