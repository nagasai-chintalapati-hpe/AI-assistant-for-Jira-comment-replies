"""Notification integrations — Teams webhook and SMTP email."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class TeamsNotifier:
    """Send AdaptiveCard notifications to Teams via incoming webhook."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        app_base_url: Optional[str] = None,
        jira_base_url: Optional[str] = None,
    ):
        from src.config import settings as _settings
        self._url = webhook_url
        self._app_base_url = (app_base_url or _settings.app.base_url).rstrip("/")
        self._jira_base_url = (jira_base_url or _settings.jira.base_url).rstrip("/")
        if self._url:
            logger.info("Teams notifier initialised (AdaptiveCard format)")

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    # Public API
    def notify_draft_generated(
        self,
        draft_id: str,
        issue_key: str,
        classification: str,
        confidence: float,
        body_preview: str,
        evidence_links: Optional[list[dict[str, str]]] = None,
        missing_info: Optional[list[str]] = None,
    ) -> bool:
        """Send a 'new draft' AdaptiveCard to Teams with evidence, checklist, and action buttons."""
        card = self._build_adaptive_card(
            title=f"🤖 New Draft — {issue_key}",
            facts={
                "Draft ID": draft_id,
                "Classification": classification.replace("_", " ").title(),
                "Confidence": f"{confidence:.0%}",
            },
            body=body_preview[:500],
            style="accent",
            draft_id=draft_id,
            issue_key=issue_key,
            evidence_links=evidence_links,
            missing_info=missing_info,
        )
        return self._send(card)

    def notify_draft_approved(
        self,
        draft_id: str,
        issue_key: str,
        approved_by: str,
    ) -> bool:
        """Send an ‘approved’ AdaptiveCard to Teams."""
        card = self._build_adaptive_card(
            title=f"Draft Approved — {issue_key}",
            facts={
                "Draft ID": draft_id,
                "Approved by": approved_by,
            },
            body="The draft has been approved and posted to Jira.",
            style="good",
            draft_id=draft_id,
            issue_key=issue_key,
        )
        return self._send(card)

    def notify_draft_rejected(
        self,
        draft_id: str,
        issue_key: str,
        feedback: str,
    ) -> bool:
        """Send a ‘rejected’ AdaptiveCard to Teams."""
        card = self._build_adaptive_card(
            title=f"Draft Rejected — {issue_key}",
            facts={
                "Draft ID": draft_id,
                "Feedback": feedback or "(none)",
            },
            body="The draft was rejected. See feedback above.",
            style="attention",
            draft_id=draft_id,
            issue_key=issue_key,
        )
        return self._send(card)

    # Internals
    def _build_adaptive_card(
        self,
        title: str,
        facts: dict[str, str],
        body: str,
        style: str = "accent",   # "accent"|"good"|"attention"|"warning"|"default"
        draft_id: str = "",
        issue_key: str = "",
        evidence_links: Optional[list[dict[str, str]]] = None,
        missing_info: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Build a Teams AdaptiveCard payload."""
        card_body: list[dict] = [
            {
                "type": "Container",
                "style": style,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": title,
                        "weight": "Bolder",
                        "size": "Medium",
                        "wrap": True,
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {"title": k, "value": v} for k, v in facts.items()
                        ],
                    },
                ],
            },
            {
                "type": "TextBlock",
                "text": body,
                "wrap": True,
                "spacing": "Medium",
            },
        ]

        # Evidence links section
        if evidence_links:
            evidence_items: list[dict] = [
                {
                    "type": "TextBlock",
                    "text": "📎 **Evidence**",
                    "weight": "Bolder",
                    "spacing": "Medium",
                    "wrap": True,
                },
            ]
            for ev in evidence_links[:5]:
                source = ev.get("source", "")
                url = ev.get("url", "")
                excerpt = ev.get("excerpt", "")[:100]
                link_text = f"[{source}]({url})" if url else source
                evidence_items.append({
                    "type": "TextBlock",
                    "text": f"• {link_text}: {excerpt}",
                    "wrap": True,
                    "size": "Small",
                })
            card_body.append({
                "type": "Container",
                "items": evidence_items,
            })

        # "What's missing" checklist
        if missing_info:
            checklist_items: list[dict] = [
                {
                    "type": "TextBlock",
                    "text": "⚠️ **What's Missing**",
                    "weight": "Bolder",
                    "spacing": "Medium",
                    "wrap": True,
                },
            ]
            for item in missing_info[:5]:
                checklist_items.append({
                    "type": "TextBlock",
                    "text": f"☐ {item}",
                    "wrap": True,
                    "size": "Small",
                })
            card_body.append({
                "type": "Container",
                "items": checklist_items,
            })

        actions: list[dict] = []
        if draft_id and self._app_base_url:
            actions.append({
                "type": "Action.OpenUrl",
                "title": "📋 Review Draft",
                "url": f"{self._app_base_url}/ui/drafts/{draft_id}",
            })
            actions.append({
                "type": "Action.OpenUrl",
                "title": "✅ Approve",
                "url": f"{self._app_base_url}/ui/drafts/{draft_id}",
            })
            actions.append({
                "type": "Action.OpenUrl",
                "title": "❌ Reject",
                "url": f"{self._app_base_url}/ui/drafts/{draft_id}",
            })
        if issue_key and self._jira_base_url:
            actions.append({
                "type": "Action.OpenUrl",
                "title": "🔗 Open in Jira",
                "url": f"{self._jira_base_url}/browse/{issue_key}",
            })

        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": card_body,
                        "actions": actions,
                    },
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

    # Public API
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

    # Internals
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


# Notification Facade
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
        evidence_links: Optional[list[dict[str, str]]] = None,
        missing_info: Optional[list[str]] = None,
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
            results["teams"] = self._teams.notify_draft_generated(
                **kwargs,
                evidence_links=evidence_links,
                missing_info=missing_info,
            )
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
