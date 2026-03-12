"""Notification integrations — Teams webhook + Email (SMTP).

Sends a summary card / email when:
  • A new draft is generated  (notify_draft_generated)
  • A draft is approved       (notify_draft_approved)
  • A draft is rejected       (notify_draft_rejected)

Both channels are **optional** — if credentials are missing the call
is silently skipped with a log message.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TeamsNotifier:
    """Send notifications to a Microsoft Teams channel via Incoming Webhook."""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv("TEAMS_WEBHOOK_URL", "")

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, draft: dict) -> bool:
        """Backward-compatible alias for draft-generated notifications."""
        return self.send_event(draft, event_name="generated")

    def send_event(
        self,
        draft: dict,
        *,
        event_name: str,
        actor: Optional[str] = None,
        feedback: str = "",
    ) -> bool:
        """Post an Adaptive Card to Teams with draft details.

        Args:
            draft: Draft dict with issue/comment metadata and generated content.

        Returns:
            True if the message was delivered, False otherwise.
        """
        if not self.is_configured:
            logger.debug("Teams notifier not configured – skipping")
            return False

        issue_key = draft.get("issue_key", "unknown")
        classification = draft.get("classification", draft.get("comment_type", "unknown"))
        confidence = draft.get("confidence_score", 0)
        draft_id = draft.get("draft_id", "")
        body_preview = (
            (draft.get("body", "")[:300] + "…")
            if len(draft.get("body", "")) > 300
            else draft.get("body", "")
        )

        event_label = {
            "generated": "Draft Reply Ready",
            "approved": "Draft Approved",
            "rejected": "Draft Rejected",
        }.get(event_name, "Draft Update")

        card = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {
                                "type": "TextBlock",
                                "size": "Medium",
                                "weight": "Bolder",
                                "text": f"{event_label} – {issue_key}",
                            },
                            {
                                "type": "FactSet",
                                "facts": [
                                    {"title": "Event", "value": event_name},
                                    {"title": "Issue", "value": issue_key},
                                    {"title": "Classification", "value": classification},
                                    {"title": "Confidence", "value": f"{confidence:.0%}"},
                                    {"title": "Draft ID", "value": draft_id},
                                ],
                            },
                            {
                                "type": "TextBlock",
                                "text": body_preview,
                                "wrap": True,
                                "maxLines": 6,
                            },
                        ],
                    },
                }
            ],
        }

        if actor:
            card["attachments"][0]["content"]["body"].append(
                {"type": "TextBlock", "text": f"By: {actor}", "wrap": True}
            )
        if feedback:
            card["attachments"][0]["content"]["body"].append(
                {"type": "TextBlock", "text": f"Feedback: {feedback[:300]}", "wrap": True}
            )

        try:
            resp = requests.post(
                self.webhook_url,
                json=card,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code in (200, 202):
                logger.info("Teams notification sent for %s", issue_key)
                return True
            logger.warning("Teams webhook returned %s: %s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:
            logger.error("Teams notification failed: %s", exc)
            return False


class EmailNotifier:
    """Send notification emails via SMTP."""

    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_username: Optional[str] = None,
        smtp_password: Optional[str] = None,
        email_from: Optional[str] = None,
        email_to: Optional[str] = None,
    ):
        self.smtp_host = smtp_host or os.getenv("SMTP_HOST", "")
        self.smtp_port = smtp_port or int(os.getenv("SMTP_PORT", "587"))
        self.smtp_username = smtp_username or os.getenv("SMTP_USERNAME", "")
        self.smtp_password = smtp_password or os.getenv("SMTP_PASSWORD", "")
        self.email_from = email_from or os.getenv("EMAIL_FROM", "")
        self.email_to = email_to or os.getenv("EMAIL_TO", "")

    @property
    def is_configured(self) -> bool:
        return bool(
            self.smtp_host and self.smtp_username and self.smtp_password and self.email_from
        )

    def send(self, draft: dict) -> bool:
        """Backward-compatible alias for draft-generated notifications."""
        return self.send_event(draft, event_name="generated")

    def send_event(
        self,
        draft: dict,
        *,
        event_name: str,
        actor: Optional[str] = None,
        feedback: str = "",
    ) -> bool:
        """Send an email notification about a draft.

        Args:
            draft: Draft dict.

        Returns:
            True if email was sent, False otherwise.
        """
        if not self.is_configured:
            logger.debug("Email notifier not configured – skipping")
            return False

        issue_key = draft.get("issue_key", "unknown")
        classification = draft.get("classification", draft.get("comment_type", "unknown"))
        confidence = draft.get("confidence_score", 0)
        draft_id = draft.get("draft_id", "")
        body_text = draft.get("body", "")

        event_title = {
            "generated": "Draft reply ready",
            "approved": "Draft approved",
            "rejected": "Draft rejected",
        }.get(event_name, "Draft update")

        recipients = [r.strip() for r in self.email_to.split(",") if r.strip()]
        if not recipients:
            logger.warning("No EMAIL_TO recipients configured")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Jira Assistant] {event_title} – {issue_key} ({classification})"
        msg["From"] = self.email_from
        msg["To"] = ", ".join(recipients)

        text_body = (
            f"A draft update was recorded for {issue_key}.\n\n"
            f"Event: {event_name}\n"
            f"Classification: {classification}\n"
            f"Confidence: {confidence:.0%}\n"
            f"Draft ID: {draft_id}\n\n"
            f"--- Draft ---\n{body_text}\n"
        )

        if actor:
            text_body += f"\nActor: {actor}\n"
        if feedback:
            text_body += f"Feedback: {feedback}\n"

        msg.attach(MIMEText(text_body, "plain"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                server.starttls()
                server.login(self.smtp_username, self.smtp_password)
                server.sendmail(self.email_from, recipients, msg.as_string())
            logger.info("Email notification sent for %s to %s", issue_key, recipients)
            return True
        except Exception as exc:
            logger.error("Email notification failed: %s", exc)
            return False


def notify_draft_ready(draft: dict) -> dict[str, bool]:
    """Backward-compatible helper for generated-draft notifications."""
    return notify_draft_event(draft, event_name="generated")


def notify_draft_event(
    draft: dict,
    *,
    event_name: str,
    actor: Optional[str] = None,
    feedback: str = "",
) -> dict[str, bool]:
    """Send notifications on all configured channels.

    Returns:
        Dict mapping channel name to success boolean.
    """
    results: dict[str, bool] = {}

    teams = TeamsNotifier()
    if teams.is_configured:
        results["teams"] = teams.send_event(
            draft,
            event_name=event_name,
            actor=actor,
            feedback=feedback,
        )

    email = EmailNotifier()
    if email.is_configured:
        results["email"] = email.send_event(
            draft,
            event_name=event_name,
            actor=actor,
            feedback=feedback,
        )

    if not results:
        logger.info("No notification channels configured – draft stored only")

    return results
