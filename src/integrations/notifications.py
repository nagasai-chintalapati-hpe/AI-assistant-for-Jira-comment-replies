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
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


#  Teams Webhook Notifier    
class TeamsNotifier:
    """Send adaptive-card style notifications to a Microsoft Teams channel
    via an incoming webhook URL.
    """

    def __init__(self, webhook_url: Optional[str] = None):
        self._url = webhook_url
        if self._url:
            logger.info("Teams notifier initialised")

    @property
    def enabled(self) -> bool:
        return bool(self._url)
    
    #  Public helpers    
    def notify_draft_generated(
        self,
        draft_id: str,
        issue_key: str,
        classification: str,
        confidence: float,
        body_preview: str,
    ) -> bool:
        """Send a 'new draft' card to Teams."""
        card = self._build_card(
            title=f" New Draft — {issue_key}",
            facts={
                "Draft ID": draft_id,
                "Classification": classification,
                "Confidence": f"{confidence:.0%}",
            },
            body=body_preview[:500],
            color="0078D7",
        )
        return self._send(card)

    def notify_draft_approved(
        self,
        draft_id: str,
        issue_key: str,
        approved_by: str,
    ) -> bool:
        """Send an 'approved' card to Teams."""
        card = self._build_card(
            title=f"Draft Approved — {issue_key}",
            facts={
                "Draft ID": draft_id,
                "Approved by": approved_by,
            },
            body="The draft has been approved and is ready to post.",
            color="00C851",
        )
        return self._send(card)

    def notify_draft_rejected(
        self,
        draft_id: str,
        issue_key: str,
        feedback: str,
    ) -> bool:
        """Send a 'rejected' card to Teams."""
        card = self._build_card(
            title=f" Draft Rejected — {issue_key}",
            facts={
                "Draft ID": draft_id,
                "Feedback": feedback or "(none)",
            },
            body="The draft was rejected. See feedback above.",
            color="FF4444",
        )
        return self._send(card)
    
    #  Internals 
    @staticmethod
    def _build_card(
        title: str,
        facts: dict[str, str],
        body: str,
        color: str = "0078D7",
    ) -> dict[str, Any]:
        """Build a Teams MessageCard payload."""
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": color,
            "summary": title,
            "sections": [
                {
                    "activityTitle": title,
                    "facts": [
                        {"name": k, "value": v} for k, v in facts.items()
                    ],
                    "text": body,
                    "markdown": True,
                }
            ],
        }

    def _send(self, payload: dict[str, Any]) -> bool:
        """POST the payload to the Teams webhook URL."""
        if not self._url:
            logger.debug("Teams notifier disabled (no webhook URL)")
            return False
        try:
            resp = requests.post(
                self._url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Teams notification sent")
            return True
        except Exception as exc:
            logger.warning("Teams notification failed: %s", exc)
            return False

#  Email (SMTP) Notifier        
class EmailNotifier:
    """Send plain-text + HTML email notifications via SMTP."""

    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: int = 587,
        smtp_username: Optional[str] = None,
        smtp_password: Optional[str] = None,
        from_address: Optional[str] = None,
        to_addresses: Optional[list[str]] = None,
        use_tls: bool = True,
    ):
        self._host = smtp_host
        self._port = smtp_port
        self._username = smtp_username
        self._password = smtp_password
        self._from = from_address
        self._to = to_addresses or []
        self._use_tls = use_tls
        if self._host:
            logger.info("Email notifier initialised (host=%s)", self._host)

    @property
    def enabled(self) -> bool:
        return bool(self._host and self._from and self._to)

    #  Public helpers  
    def notify_draft_generated(
        self,
        draft_id: str,
        issue_key: str,
        classification: str,
        confidence: float,
        body_preview: str,
    ) -> bool:
        subject = f"[Jira Assistant] New Draft — {issue_key}"
        html = (
            f"<h2>New Draft Generated</h2>"
            f"<p><b>Issue:</b> {issue_key}<br>"
            f"<b>Draft ID:</b> {draft_id}<br>"
            f"<b>Classification:</b> {classification}<br>"
            f"<b>Confidence:</b> {confidence:.0%}</p>"
            f"<h3>Preview</h3><pre>{body_preview[:1000]}</pre>"
        )
        return self._send_email(subject, html)

    def notify_draft_approved(
        self,
        draft_id: str,
        issue_key: str,
        approved_by: str,
    ) -> bool:
        subject = f"[Jira Assistant] Draft Approved — {issue_key}"
        html = (
            f"<h2>Draft Approved</h2>"
            f"<p><b>Issue:</b> {issue_key}<br>"
            f"<b>Draft ID:</b> {draft_id}<br>"
            f"<b>Approved by:</b> {approved_by}</p>"
        )
        return self._send_email(subject, html)

    def notify_draft_rejected(
        self,
        draft_id: str,
        issue_key: str,
        feedback: str,
    ) -> bool:
        subject = f"[Jira Assistant] Draft Rejected — {issue_key}"
        html = (
            f"<h2>Draft Rejected</h2>"
            f"<p><b>Issue:</b> {issue_key}<br>"
            f"<b>Draft ID:</b> {draft_id}<br>"
            f"<b>Feedback:</b> {feedback or '(none)'}</p>"
        )
        return self._send_email(subject, html)

    #  Internals 
    def _send_email(self, subject: str, html_body: str) -> bool:
        """Send an HTML email via SMTP."""
        if not self.enabled:
            logger.debug("Email notifier disabled (missing config)")
            return False
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self._from
            msg["To"] = ", ".join(self._to)

            # Plain-text fallback
            import re
            plain = html_body.replace("<br>", "\n").replace("</p>", "\n")
            plain = re.sub(r"<[^>]+>", "", plain)

            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            with smtplib.SMTP(self._host, self._port) as server:
                if self._use_tls:
                    server.starttls()
                if self._username and self._password:
                    server.login(self._username, self._password)
                server.sendmail(self._from, self._to, msg.as_string())

            logger.info("Email notification sent to %s", self._to)
            return True
        except Exception as exc:
            logger.warning("Email notification failed: %s", exc)
            return False


# ===================================================================== #
#  Unified Notifier Facade                                               #
# ===================================================================== #

class NotificationService:
    """Facade that fans out to Teams + Email (both optional)."""

    def __init__(
        self,
        teams: Optional[TeamsNotifier] = None,
        email: Optional[EmailNotifier] = None,
    ):
        self._teams = teams
        self._email = email

    @property
    def any_enabled(self) -> bool:
        return (
            (self._teams is not None and self._teams.enabled)
            or (self._email is not None and self._email.enabled)
        )

    def notify_draft_generated(
        self,
        draft_id: str,
        issue_key: str,
        classification: str,
        confidence: float,
        body_preview: str,
    ) -> dict[str, bool]:
        """Notify all enabled channels about a new draft."""
        results: dict[str, bool] = {}
        kwargs = dict(
            draft_id=draft_id,
            issue_key=issue_key,
            classification=classification,
            confidence=confidence,
            body_preview=body_preview,
        )
        if self._teams and self._teams.enabled:
            results["teams"] = self._teams.notify_draft_generated(**kwargs)
        if self._email and self._email.enabled:
            results["email"] = self._email.notify_draft_generated(**kwargs)
        return results

    def notify_draft_approved(
        self,
        draft_id: str,
        issue_key: str,
        approved_by: str,
    ) -> dict[str, bool]:
        results: dict[str, bool] = {}
        kwargs = dict(draft_id=draft_id, issue_key=issue_key, approved_by=approved_by)
        if self._teams and self._teams.enabled:
            results["teams"] = self._teams.notify_draft_approved(**kwargs)
        if self._email and self._email.enabled:
            results["email"] = self._email.notify_draft_approved(**kwargs)
        return results

    def notify_draft_rejected(
        self,
        draft_id: str,
        issue_key: str,
        feedback: str,
    ) -> dict[str, bool]:
        results: dict[str, bool] = {}
        kwargs = dict(draft_id=draft_id, issue_key=issue_key, feedback=feedback)
        if self._teams and self._teams.enabled:
            results["teams"] = self._teams.notify_draft_rejected(**kwargs)
        if self._email and self._email.enabled:
            results["email"] = self._email.notify_draft_rejected(**kwargs)
        return results
