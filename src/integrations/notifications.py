"""Notification integrations — Teams webhook + Email (SMTP).

MVP v1 sends a summary card / email when:
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
            title=f"📝 New Draft — {issue_key}",
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
