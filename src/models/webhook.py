"""Webhook event models for incoming Jira payloads."""

from typing import Optional, Any, Union
from pydantic import BaseModel, Field, field_validator


def _adf_to_text(adf: dict) -> str:
    """Recursively extract plain text from ADF (Atlassian Document Format)."""
    if not isinstance(adf, dict):
        return str(adf)
    parts: list[str] = []
    if adf.get("type") == "text":
        parts.append(adf.get("text", ""))
    for child in adf.get("content", []):
        parts.append(_adf_to_text(child))
    text = "".join(parts)
    if adf.get("type") in ("paragraph", "heading", "bulletList", "orderedList", "listItem", "codeBlock"):
        text = text.strip() + "\n"
    return text


class WebhookUser(BaseModel):
    """Jira user from webhook payload"""
    accountId: Optional[str] = None
    displayName: Optional[str] = None
    emailAddress: Optional[str] = None
    active: Optional[bool] = True


class WebhookComment(BaseModel):
    """Comment section of a Jira webhook event"""
    id: str
    body: Union[str, dict, Any] = ""
    author: WebhookUser
    created: str
    updated: str

    @field_validator("body", mode="before")
    @classmethod
    def normalise_body(cls, v: Any) -> str:
        """Accept ADF dict or plain string — always return a string."""
        if isinstance(v, dict):
            return _adf_to_text(v).strip()
        return str(v) if v else ""


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
    id: Optional[str] = None
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
